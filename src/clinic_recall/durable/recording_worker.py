"""Finite durable worker for deterministic recording start and stop effects."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from ..db import clinic_scope, get_sessionmaker, tenant_select
from ..enums import (
    CallRecordingStatus,
    Channel,
    ClinicPhoneProvider,
    ExternalEffectState,
    ExternalEffectType,
    InboundCallStatus,
    RecordingConsentState,
)
from ..models import CallRecord, ExternalEffect, InboundCall
from ..pilot_controls import (
    evaluate_recording_gate,
    job_gate_for_snapshot,
    operational_switch_snapshot_from_environment,
)
from ..recording import (
    TwilioRecordingProvider,
    enforce_recording_switch_off,
    recording_disclosure_from_environment,
)
from ..rights import SubjectFrozenError, assert_patient_writable
from ..telemetry import emit_worker_summary
from .config import durable_recording_enabled, durable_recording_provider_is_twilio
from .effects import (
    claim_effects,
    lock_dispatching_effect,
    mark_canceled,
    mark_dispatching,
    mark_reconcile_required,
    mark_rejected,
    mark_succeeded,
)
from .worker import _bootstrap_runtime_configuration

SessionFactory = Callable[[], Session]
StartGate = Callable[[Session, str, CallRecord, datetime], bool]

_CALL_SID_PATTERN = re.compile(r"CA[0-9a-fA-F]{32}\Z")
_RECORDING_SID_PATTERN = re.compile(r"RE[0-9a-fA-F]{32}\Z")
_START_INTENT = "recording_start"
_STOP_INTENT = "recording_stop"
_CLOSED_REASON_CODES = frozenset(
    {
        "provider_accepted",
        "provider_rejected",
        "transport_error",
        "provider_server_error",
        "malformed_response",
        "missing_recording_sid",
        "provider_identity_conflict",
        "provider_state_unconfirmed",
    }
)


class RecordingProvider(Protocol):
    """Provider-neutral recording control used by the finite worker."""

    name: str

    def start_recording(self, *, call_sid: str, callback_url: str) -> Any: ...

    def stop_recording(self, *, call_sid: str, recording_sid: str) -> Any: ...


@dataclass(frozen=True)
class RecordingRunOnceResult:
    """Aggregate-only outcome from one bounded recording invocation."""

    enabled: bool
    claimed: int = 0
    started: int = 0
    stopped: int = 0
    rejected: int = 0
    canceled: int = 0
    reconcile_required: int = 0

    def as_summary(self) -> dict[str, int | bool]:
        """Return counters containing no clinic, call, or provider identifiers."""
        return {
            "enabled": self.enabled,
            "claimed": self.claimed,
            "started": self.started,
            "stopped": self.stopped,
            "rejected": self.rejected,
            "canceled": self.canceled,
            "reconcile_required": self.reconcile_required,
        }


def run_once(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    worker_id: str,
    provider: RecordingProvider,
    start_gate: StartGate,
    now: datetime,
    enabled: bool = False,
    lease_for: timedelta = timedelta(minutes=5),
    limit: int = 10,
) -> RecordingRunOnceResult:
    """Claim and dispatch one finite recording batch without automatic replay."""
    if not enabled:
        return RecordingRunOnceResult(enabled=False)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not clinic_id or not worker_id:
        raise ValueError("clinic_id and worker_id are required")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    now = now.astimezone(UTC)

    with session_factory() as session:
        claimed = claim_effects(
            session,
            clinic_id=clinic_id,
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
            limit=limit,
            effect_types=(ExternalEffectType.RECORDING,),
        )
        effect_ids = [effect.id for effect in claimed]
        for effect_id in effect_ids:
            mark_dispatching(
                session,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
            )
        session.commit()

    started = 0
    stopped = 0
    rejected = 0
    canceled = 0
    reconcile_required = 0
    for effect_id in effect_ids:
        try:
            with session_factory() as session:
                effect = lock_dispatching_effect(
                    session,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    worker_id=worker_id,
                )
                record = _load_record(session, clinic_id, effect)
                intent = _valid_intent(effect, record)
                blocked_reason = _blocked_reason(
                    session,
                    clinic_id=clinic_id,
                    effect=effect,
                    record=record,
                    intent=intent,
                    start_gate=start_gate,
                    now=now,
                )
                if blocked_reason:
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code=blocked_reason,
                    )
                    if intent == _START_INTENT and record is not None:
                        record.recording_status = CallRecordingStatus.ABSENT
                    session.commit()
                    canceled += 1
                    continue

                if intent is None:
                    raise RuntimeError("validated recording intent is missing")
                assert record is not None
                assert record.provider_call_id is not None
                call_sid = record.provider_call_id
                recording_sid_for_stop = record.recording_sid
                callback_url = _recording_callback_url(effect)
                if intent == _START_INTENT:
                    record.recording_status = CallRecordingStatus.STARTING
                else:
                    assert recording_sid_for_stop is not None
                    record.recording_status = CallRecordingStatus.STOPPING
                session.commit()

            if intent == _START_INTENT:
                result = provider.start_recording(
                    call_sid=call_sid,
                    callback_url=callback_url,
                )
            else:
                assert recording_sid_for_stop is not None
                result = provider.stop_recording(
                    call_sid=call_sid,
                    recording_sid=recording_sid_for_stop,
                )

            with session_factory() as session:
                effect = _load_effect_for_settlement(session, clinic_id, effect_id)
                record = _load_record(session, clinic_id, effect, for_update=True)
                if record is None:
                    raise LookupError("recording call record is missing")
                current_intent = _valid_intent(effect, record)
                if current_intent != intent:
                    raise ValueError("recording effect contract changed during dispatch")

                disposition = _closed_disposition(result)
                recording_sid = _result_recording_sid(result)
                if disposition == "accepted" and _accepted_identity(
                    intent,
                    record,
                    recording_sid,
                ):
                    settled = mark_succeeded(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        provider_resource_id=str(recording_sid),
                    )
                    if settled.state == ExternalEffectState.SUCCEEDED:
                        if intent == _START_INTENT:
                            assert recording_sid is not None
                            _settle_accepted_start(
                                session,
                                clinic_id=clinic_id,
                                record=record,
                                recording_sid=recording_sid,
                                start_gate=start_gate,
                                now=now,
                            )
                            started += 1
                        else:
                            if record.recording_status not in {
                                CallRecordingStatus.STORED,
                                CallRecordingStatus.ABSENT,
                            }:
                                record.recording_status = CallRecordingStatus.COMPLETED
                            record.recording_stopped_at = record.recording_stopped_at or now
                            record.recording_stop_requested_at = (
                                record.recording_stop_requested_at or now
                            )
                            stopped += 1
                    else:
                        record.recording_status = CallRecordingStatus.RECONCILE_REQUIRED
                        reconcile_required += 1
                elif disposition == "rejected":
                    if effect.state == ExternalEffectState.DISPATCHING:
                        mark_rejected(
                            session,
                            clinic_id=clinic_id,
                            effect_id=effect_id,
                            worker_id=worker_id,
                            now=now,
                            reason_code=_closed_reason(result, "provider_rejected"),
                        )
                        record.recording_status = (
                            CallRecordingStatus.ABSENT
                            if intent == _START_INTENT
                            else CallRecordingStatus.RECONCILE_REQUIRED
                        )
                        rejected += 1
                    else:
                        _mark_settlement_conflict(effect, record)
                        reconcile_required += 1
                elif effect.state == ExternalEffectState.SUCCEEDED:
                    if intent == _START_INTENT and effect.provider_resource_id:
                        _settle_accepted_start(
                            session,
                            clinic_id=clinic_id,
                            record=record,
                            recording_sid=effect.provider_resource_id,
                            start_gate=start_gate,
                            now=now,
                        )
                        started += 1
                    elif intent == _STOP_INTENT:
                        stopped += 1
                elif effect.state == ExternalEffectState.DISPATCHING:
                    mark_reconcile_required(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code=_closed_reason(
                            result,
                            (
                                "missing_recording_sid"
                                if disposition == "accepted"
                                else "provider_outcome_unknown"
                            ),
                        ),
                    )
                    if record.recording_status not in {
                        CallRecordingStatus.COMPLETED,
                        CallRecordingStatus.STORED,
                        CallRecordingStatus.ABSENT,
                    }:
                        record.recording_status = CallRecordingStatus.RECONCILE_REQUIRED
                    reconcile_required += 1
                else:
                    record.recording_status = CallRecordingStatus.RECONCILE_REQUIRED
                    reconcile_required += 1
                session.commit()
        except Exception:  # noqa: BLE001 - any post-dispatch uncertainty must not replay
            with session_factory() as session:
                effect = _load_effect_for_settlement(session, clinic_id, effect_id)
                if effect.state == ExternalEffectState.DISPATCHING:
                    mark_reconcile_required(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                    )
                with clinic_scope(session, clinic_id):
                    record = session.execute(
                        tenant_select(CallRecord).where(CallRecord.id == effect.aggregate_id)
                    ).scalar_one_or_none()
                    if record is not None and record.recording_status not in {
                        CallRecordingStatus.COMPLETED,
                        CallRecordingStatus.STORED,
                        CallRecordingStatus.ABSENT,
                    }:
                        record.recording_status = CallRecordingStatus.RECONCILE_REQUIRED
                session.commit()
            reconcile_required += 1

    result = RecordingRunOnceResult(
        enabled=True,
        claimed=len(effect_ids),
        started=started,
        stopped=stopped,
        rejected=rejected,
        canceled=canceled,
        reconcile_required=reconcile_required,
    )
    emit_worker_summary("recording_dispatch", result.as_summary())
    return result


def _load_record(
    session: Session,
    clinic_id: str,
    effect: ExternalEffect,
    *,
    for_update: bool = False,
) -> CallRecord | None:
    with clinic_scope(session, clinic_id):
        statement = tenant_select(CallRecord).where(CallRecord.id == effect.aggregate_id)
        if for_update and session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        return session.execute(statement).scalar_one_or_none()


def _load_effect_for_settlement(
    session: Session,
    clinic_id: str,
    effect_id: str,
) -> ExternalEffect:
    with clinic_scope(session, clinic_id):
        statement = tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        effect = session.execute(statement).scalar_one_or_none()
    if effect is None:
        raise LookupError("recording external effect is missing")
    return effect


def _settle_accepted_start(
    session: Session,
    *,
    clinic_id: str,
    record: CallRecord,
    recording_sid: str,
    start_gate: StartGate,
    now: datetime,
) -> None:
    if record.recording_sid not in {None, recording_sid}:
        record.recording_status = CallRecordingStatus.RECONCILE_REQUIRED
        return
    record.recording_sid = recording_sid
    record.recording_started_at = record.recording_started_at or now
    terminal_or_stopping = record.recording_status in {
        CallRecordingStatus.STOP_PENDING,
        CallRecordingStatus.STOPPING,
        CallRecordingStatus.COMPLETED,
        CallRecordingStatus.STORED,
        CallRecordingStatus.ABSENT,
    }
    if terminal_or_stopping:
        return
    stop_required = (
        record.consent_state != RecordingConsentState.GRANTED
        or record.recording_stop_requested_at is not None
        or not start_gate(session, clinic_id, record, now)
    )
    record.recording_status = CallRecordingStatus.IN_PROGRESS
    if stop_required:
        from ..recording import request_recording_stop

        request_recording_stop(
            session,
            clinic_id=clinic_id,
            call_record_id=record.id,
            now=now,
        )


def _mark_settlement_conflict(effect: ExternalEffect, record: CallRecord) -> None:
    effect.state = ExternalEffectState.RECONCILE_REQUIRED
    effect.last_error_class = "ProviderSettlementConflict"
    effect.last_error_code = "provider_outcome_conflict"
    effect.lease_owner = None
    effect.lease_expires_at = None
    record.recording_status = CallRecordingStatus.RECONCILE_REQUIRED


def _valid_intent(effect: ExternalEffect, record: CallRecord | None) -> str | None:
    if (
        record is None
        or effect.effect_type != ExternalEffectType.RECORDING
        or effect.aggregate_type != "call_record"
        or effect.aggregate_id != record.id
        or effect.payload_version != 1
        or effect.max_attempts != 1
        or not isinstance(effect.payload, dict)
    ):
        return None
    intent = effect.payload.get("intent")
    if intent not in {_START_INTENT, _STOP_INTENT}:
        return None
    if effect.payload != {"intent": intent, "call_record_id": record.id}:
        return None
    return str(intent)


def _blocked_reason(
    session: Session,
    *,
    clinic_id: str,
    effect: ExternalEffect,
    record: CallRecord | None,
    intent: str | None,
    start_gate: StartGate,
    now: datetime,
) -> str:
    if intent is None or record is None:
        return "invalid_effect_contract"
    if record.provider != ClinicPhoneProvider.TWILIO:
        return "recording_provider_unsupported"
    if not _valid_call_sid(record.provider_call_id):
        return "provider_call_identity_missing"
    if intent == _STOP_INTENT:
        if record.recording_status != CallRecordingStatus.STOP_PENDING:
            return "recording_stop_not_pending"
        if not _valid_recording_sid(record.recording_sid):
            return "recording_identity_missing"
        return ""
    if record.patient_id:
        try:
            assert_patient_writable(session, clinic_id, record.patient_id)
        except SubjectFrozenError:
            return "subject_frozen"
    if record.consent_state != RecordingConsentState.GRANTED:
        return "recording_consent_not_granted"
    if record.recording_status != CallRecordingStatus.START_PENDING:
        return "recording_start_not_pending"
    if not start_gate(session, clinic_id, record, now):
        return "recording_start_gate_closed"
    if not _base_url():
        return "recording_callback_unconfigured"
    return ""


def _recording_callback_url(effect: ExternalEffect) -> str:
    query = urlencode({"effect_token": effect.callback_token})
    return f"{_base_url()}/api/v1/voice/twilio/recording-status?{query}"


def _base_url() -> str:
    value = (
        (os.getenv("TWILIO_WEBHOOK_BASE_URL") or os.getenv("BASE_URL") or "").strip().rstrip("/")
    )
    return value if value.startswith("https://") else ""


def _valid_call_sid(value: str | None) -> bool:
    return bool(value and _CALL_SID_PATTERN.fullmatch(value))


def _valid_recording_sid(value: str | None) -> bool:
    return bool(value and _RECORDING_SID_PATTERN.fullmatch(value))


def _closed_disposition(result: Any) -> str:
    value = getattr(getattr(result, "disposition", None), "value", None)
    if value is None:
        value = getattr(result, "disposition", None)
    return str(value) if value in {"accepted", "rejected", "ambiguous"} else "ambiguous"


def _result_recording_sid(result: Any) -> str | None:
    value = getattr(result, "recording_sid", None)
    return str(value) if isinstance(value, str) else None


def _accepted_identity(
    intent: str,
    record: CallRecord,
    recording_sid: str | None,
) -> bool:
    if not _valid_recording_sid(recording_sid):
        return False
    return intent == _START_INTENT or recording_sid == record.recording_sid


def _closed_reason(result: Any, default: str) -> str:
    reason = getattr(getattr(result, "reason", None), "value", None)
    if reason is None:
        reason = getattr(result, "reason", None)
    return str(reason) if reason in _CLOSED_REASON_CODES else default


def run_runtime_batch(
    *,
    clinic_id: str,
    worker_id: str | None = None,
    now: datetime | None = None,
    limit: int = 10,
) -> RecordingRunOnceResult | None:
    """Run one configured batch and enforce switch-off before claiming effects."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if not durable_recording_provider_is_twilio():
        return None
    provider = _runtime_provider()
    if provider is None:
        return None
    session_factory = get_sessionmaker()
    switches = operational_switch_snapshot_from_environment()
    with session_factory.begin() as session:
        decision = evaluate_recording_gate(
            session,
            clinic_id=clinic_id,
            switches=switches,
            now=current,
        )
        if not durable_recording_enabled() or not decision.allowed:
            enforce_recording_switch_off(
                session,
                clinic_id=clinic_id,
                now=current,
                reason_code="recording_gate_closed",
            )
    return run_once(
        session_factory,
        clinic_id=clinic_id,
        worker_id=worker_id or _default_worker_id(),
        provider=provider,
        start_gate=_runtime_start_gate,
        now=current,
        enabled=True,
        limit=limit,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one finite durable recording start/stop batch."""
    parser = argparse.ArgumentParser(description="Run one durable Clinic Recall recording batch.")
    parser.add_argument("--clinic-id", required=True, help="Internal clinic scope identifier.")
    parser.add_argument("--worker-id", default=None, help="Lease owner; defaults to host.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum effects to claim.")
    parser.add_argument(
        "--now",
        default=None,
        help="Timezone-aware ISO-8601 timestamp; defaults to current UTC time.",
    )
    args = parser.parse_args(argv)

    now = _parse_now(args.now)
    _bootstrap_runtime_configuration()
    if not durable_recording_provider_is_twilio():
        print(json.dumps(RecordingRunOnceResult(enabled=False).as_summary(), sort_keys=True))
        return 2 if durable_recording_enabled() else 0
    result = run_runtime_batch(
        clinic_id=args.clinic_id,
        worker_id=args.worker_id or _default_worker_id(),
        now=now,
        limit=args.limit,
    )
    if result is None:
        print(json.dumps(RecordingRunOnceResult(enabled=False).as_summary(), sort_keys=True))
        return 2
    print(json.dumps(result.as_summary(), sort_keys=True))
    return 0


def _runtime_provider() -> TwilioRecordingProvider | None:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not account_sid or not auth_token:
        return None
    return TwilioRecordingProvider(
        account_sid=account_sid,
        auth_token=auth_token,
        api_base_url=os.getenv("TWILIO_API_BASE_URL") or None,
    )


def _runtime_start_gate(
    session: Session,
    clinic_id: str,
    record: CallRecord,
    now: datetime,
) -> bool:
    if not durable_recording_enabled():
        return False
    if record.ended_at is not None:
        return False
    disclosure = recording_disclosure_from_environment(now=now)
    if disclosure is None or disclosure.version != record.consent_version:
        return False
    switches = operational_switch_snapshot_from_environment()
    if not evaluate_recording_gate(
        session,
        clinic_id=clinic_id,
        switches=switches,
        now=now,
    ).allowed:
        return False
    if record.direction.value == "inbound":
        if not record.inbound_call_id:
            return False
        with clinic_scope(session, clinic_id):
            inbound = session.execute(
                tenant_select(InboundCall).where(
                    InboundCall.id == record.inbound_call_id,
                )
            ).scalar_one_or_none()
            return bool(
                inbound is not None
                and inbound.status in {InboundCallStatus.STARTED, InboundCallStatus.STREAMING}
            )
    if not record.external_effect_id:
        return False
    with clinic_scope(session, clinic_id):
        call_effect = session.execute(
            tenant_select(ExternalEffect).where(
                ExternalEffect.id == record.external_effect_id,
                ExternalEffect.effect_type == ExternalEffectType.CALL,
            )
        ).scalar_one_or_none()
        if call_effect is None:
            return False
        if call_effect.state not in {
            ExternalEffectState.SUCCEEDED,
            ExternalEffectState.RECONCILE_REQUIRED,
        }:
            return False
        if call_effect.provider_status in {
            "call_completed",
            "call_failed",
            "non_human_confirmed",
            "provider_unresolved",
        }:
            return False
        from ..models import OutreachJob

        job = session.execute(
            tenant_select(OutreachJob).where(OutreachJob.id == call_effect.aggregate_id)
        ).scalar_one_or_none()
        if job is None:
            return False
        return job_gate_for_snapshot(switches, Channel.CALL)(
            session,
            clinic_id,
            job,
            now,
        ).allowed


def _default_worker_id() -> str:
    value = os.getenv("CONTAINER_APP_JOB_EXECUTION_NAME") or socket.gethostname()
    return value.strip()[:128] or "clinic-recall-recording-worker"


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
