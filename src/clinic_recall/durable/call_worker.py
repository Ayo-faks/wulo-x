"""Finite durable worker for Clinic Recall CALL effects."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..candidate_queue import _patient_view, clinic_config_from_row
from ..db import clinic_scope, get_sessionmaker, tenant_select
from ..eligibility import evaluate
from ..enums import (
    Channel,
    ClinicPhoneProvider,
    ExternalEffectState,
    ExternalEffectType,
    InteractionDirection,
)
from ..messaging.history import contact_history_for_send
from ..models import Clinic, ExternalEffect, OutreachJob, Patient
from ..pilot_controls import (
    job_gate_for_snapshot,
    mark_participant_contact_started,
    operational_switch_snapshot_from_environment,
    pilot_gate_decision,
)
from ..recording import bind_call_record_provider_identity, ensure_call_record
from ..rights import SubjectFrozenError, assert_patient_writable
from ..telemetry import emit_worker_summary
from ..voice_planner import ProgrammeGate, _voice_stop_reason
from ..voice_worker import (
    CallInitiationDisposition,
    CallInitiationReason,
    CallInitiationResult,
    CallInitiator,
    build_call_initiator,
)
from .config import durable_call_enabled, durable_call_provider_is_twilio
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
_CALL_SID_PATTERN = re.compile(r"CA[0-9a-fA-F]{32}\Z")
_EXPECTED_PAYLOAD = {
    "intent": "recall_fallback",
}


@dataclass(frozen=True)
class CallRunOnceResult:
    """Aggregate-only outcome from one bounded CALL worker invocation."""

    enabled: bool
    claimed: int = 0
    provider_accepted: int = 0
    rejected: int = 0
    canceled: int = 0
    reconcile_required: int = 0

    def as_summary(self) -> dict[str, int | bool]:
        """Return counters that contain no patient or provider identifiers."""
        return {
            "enabled": self.enabled,
            "claimed": self.claimed,
            "provider_accepted": self.provider_accepted,
            "rejected": self.rejected,
            "canceled": self.canceled,
            "reconcile_required": self.reconcile_required,
        }


@dataclass(frozen=True)
class _DispatchFacts:
    target_number: str
    context: dict[str, Any]
    participant_id: str | None = None


def run_once(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    worker_id: str,
    initiator: CallInitiator,
    programme_gate: ProgrammeGate | None,
    now: datetime,
    enabled: bool = False,
    lease_for: timedelta = timedelta(minutes=5),
    limit: int = 10,
) -> CallRunOnceResult:
    """Claim and dispatch one finite CALL batch without automatic retries."""
    if not enabled:
        return CallRunOnceResult(enabled=False)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not clinic_id or not worker_id:
        raise ValueError("clinic_id and worker_id are required")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if programme_gate is None:
        return CallRunOnceResult(enabled=False)
    now = now.astimezone(UTC)

    with session_factory() as session:
        claimed = claim_effects(
            session,
            clinic_id=clinic_id,
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
            limit=limit,
            effect_types=(ExternalEffectType.CALL,),
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

    provider_accepted = 0
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
                dispatch_facts, blocked_reason = _fresh_dispatch_facts(
                    session,
                    effect=effect,
                    clinic_id=clinic_id,
                    programme_gate=programme_gate,
                    now=now,
                )
                if dispatch_facts is None:
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code=blocked_reason,
                    )
                    session.commit()
                    canceled += 1
                    continue

                call_record = ensure_call_record(
                    session,
                    clinic_id,
                    provider=ClinicPhoneProvider.TWILIO,
                    provider_call_id=None,
                    external_effect_id=effect.id,
                    session_id=None,
                    direction=InteractionDirection.OUTBOUND,
                    scenario="rebooking",
                    patient_id=str(dispatch_facts.context["patient_id"]),
                    consent_snapshot=None,
                    now=now,
                )
                call_record_id = call_record.id
                session.commit()

            with session_factory() as session:
                lock_dispatching_effect(
                    session,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    worker_id=worker_id,
                )
                result = initiator.initiate_call(
                    target_number=dispatch_facts.target_number,
                    context=dispatch_facts.context,
                )
                disposition = _closed_disposition(result)
                if disposition == CallInitiationDisposition.ACCEPTED and _valid_call_sid(
                    result.call_id
                ):
                    settled = mark_succeeded(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        provider_resource_id=str(result.call_id),
                    )
                    if settled.state == ExternalEffectState.SUCCEEDED:
                        bind_call_record_provider_identity(
                            session,
                            clinic_id=clinic_id,
                            call_record_id=call_record_id,
                            provider=ClinicPhoneProvider.TWILIO,
                            provider_call_id=str(result.call_id),
                        )
                    if dispatch_facts.participant_id is not None:
                        mark_participant_contact_started(
                            session,
                            clinic_id=clinic_id,
                            participant_id=dispatch_facts.participant_id,
                            now=now,
                        )
                    if settled.state == ExternalEffectState.SUCCEEDED:
                        provider_accepted += 1
                    else:
                        reconcile_required += 1
                elif disposition == CallInitiationDisposition.NOT_DISPATCHED:
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code=_closed_reason(
                            result,
                            CallInitiationReason.INVALID_CONFIGURATION,
                        ),
                    )
                    canceled += 1
                elif disposition == CallInitiationDisposition.REJECTED:
                    mark_rejected(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code=_closed_reason(
                            result,
                            CallInitiationReason.PROVIDER_REJECTED,
                        ),
                    )
                    rejected += 1
                else:
                    mark_reconcile_required(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code=_closed_reason(
                            result,
                            CallInitiationReason.MISSING_CALL_SID if result.successful else None,
                        ),
                    )
                    reconcile_required += 1
                session.commit()
        except Exception:  # noqa: BLE001 - post-dispatch uncertainty must not replay
            with session_factory() as session:
                mark_reconcile_required(
                    session,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    worker_id=worker_id,
                    now=now,
                )
                session.commit()
            reconcile_required += 1

    result = CallRunOnceResult(
        enabled=True,
        claimed=len(effect_ids),
        provider_accepted=provider_accepted,
        rejected=rejected,
        canceled=canceled,
        reconcile_required=reconcile_required,
    )
    emit_worker_summary("call_dispatch", result.as_summary())
    return result


def _fresh_dispatch_facts(
    session: Session,
    *,
    effect: ExternalEffect,
    clinic_id: str,
    programme_gate: ProgrammeGate | None,
    now: datetime,
) -> tuple[_DispatchFacts | None, str]:
    if not _valid_effect_contract(effect):
        return None, "invalid_effect_contract"

    with clinic_scope(session, clinic_id):
        clinic = session.get(Clinic, clinic_id)
        job = session.execute(
            tenant_select(OutreachJob).where(OutreachJob.id == effect.aggregate_id)
        ).scalar_one_or_none()
        if clinic is None or job is None:
            return None, "outreach_context_missing"
        stop_reason = _voice_stop_reason(session, job)
        if stop_reason is not None:
            return None, stop_reason
        programme_decision = (
            pilot_gate_decision(programme_gate(session, clinic_id, job, now))
            if programme_gate is not None
            else None
        )
        if programme_decision is None:
            return None, "programme_gate_unbound"
        if not programme_decision.allowed:
            return None, programme_decision.reason

        patient = session.execute(
            tenant_select(Patient).where(Patient.id == job.patient_id)
        ).scalar_one_or_none()
        if patient is None:
            return None, "patient_missing"
        try:
            assert_patient_writable(session, clinic_id, patient.id)
        except SubjectFrozenError:
            return None, "subject_frozen"
        config = clinic_config_from_row(clinic)
        history = contact_history_for_send(
            session,
            clinic_id,
            patient.id,
            now,
            config,
        )
        decision = evaluate(_patient_view(patient), config, history, now, Channel.CALL)
        if not decision.eligible:
            reason = (
                decision.skip_reason.value if decision.skip_reason is not None else "ineligible"
            )
            return None, reason
        if not patient.phone:
            return None, "not_contactable"

        return (
            _DispatchFacts(
                target_number=patient.phone,
                context={
                    "source": "clinic_recall_voice_worker",
                    "scenario": "rebooking",
                    "clinic_id": clinic_id,
                    "patient_id": patient.id,
                    "outreach_job_id": job.id,
                    "record_call": False,
                    "effect_token": effect.callback_token,
                },
                participant_id=programme_decision.participant_id,
            ),
            "",
        )


def _valid_effect_contract(effect: ExternalEffect) -> bool:
    expected_payload = {
        **_EXPECTED_PAYLOAD,
        "outreach_job_id": effect.aggregate_id,
    }
    return (
        effect.effect_type == ExternalEffectType.CALL
        and effect.aggregate_type == "outreach_job"
        and effect.payload_version == 1
        and effect.payload == expected_payload
        and effect.max_attempts == 1
    )


def _valid_call_sid(call_id: str | None) -> bool:
    return bool(call_id and _CALL_SID_PATTERN.fullmatch(call_id))


def _closed_disposition(result: CallInitiationResult) -> CallInitiationDisposition:
    if result.disposition is None:
        return (
            CallInitiationDisposition.ACCEPTED
            if result.successful
            else CallInitiationDisposition.AMBIGUOUS
        )
    if result.successful != (result.disposition == CallInitiationDisposition.ACCEPTED):
        return CallInitiationDisposition.AMBIGUOUS
    return result.disposition


def _closed_reason(
    result: CallInitiationResult,
    default: CallInitiationReason | None,
) -> str:
    if isinstance(result.reason_code, CallInitiationReason):
        return result.reason_code.value
    if default is not None:
        return default.value
    return "provider_outcome_unknown"


def main(argv: Sequence[str] | None = None) -> int:
    """Run one finite durable CALL worker invocation."""
    parser = argparse.ArgumentParser(description="Run one durable Clinic Recall CALL batch.")
    parser.add_argument(
        "--clinic-id",
        required=True,
        help="Internal clinic scope identifier.",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="Lease owner; defaults to execution host.",
    )
    parser.add_argument("--limit", type=int, default=10, help="Maximum effects to claim.")
    parser.add_argument(
        "--now",
        default=None,
        help="Timezone-aware ISO-8601 timestamp; defaults to current UTC time.",
    )
    args = parser.parse_args(argv)

    now = _parse_now(args.now)
    _bootstrap_runtime_configuration()
    if not durable_call_enabled():
        print(json.dumps(CallRunOnceResult(enabled=False).as_summary(), sort_keys=True))
        return 0

    if not durable_call_provider_is_twilio():
        print(json.dumps(CallRunOnceResult(enabled=False).as_summary(), sort_keys=True))
        return 2

    programme_gate = _runtime_programme_gate(now)
    if programme_gate is None:
        print(json.dumps(CallRunOnceResult(enabled=False).as_summary(), sort_keys=True))
        return 2

    result = run_once(
        get_sessionmaker(),
        clinic_id=args.clinic_id,
        worker_id=args.worker_id or _default_worker_id(),
        initiator=build_call_initiator("twilio"),
        programme_gate=programme_gate,
        now=now,
        enabled=True,
        limit=args.limit,
    )
    print(json.dumps(result.as_summary(), sort_keys=True))
    return 0


def _runtime_programme_gate(now: datetime) -> ProgrammeGate | None:
    """Bind the fresh operational snapshot to the PR-13 database gate."""
    switches = operational_switch_snapshot_from_environment()
    if not switches.decision(Channel.CALL, now).allowed:
        return None
    return job_gate_for_snapshot(switches, Channel.CALL)


def _default_worker_id() -> str:
    value = os.getenv("CONTAINER_APP_JOB_EXECUTION_NAME") or socket.gethostname()
    return value.strip()[:128] or "clinic-recall-call-worker"


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
