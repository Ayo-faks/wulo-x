"""Finite worker for the first durable Clinic Recall SMS effect."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ..booking_confirmation import is_booking_confirmation_effect_authorized
from ..db import clinic_scope, get_sessionmaker, tenant_select
from ..enums import Channel, ExternalEffectState, ExternalEffectType
from ..identity_evidence import IdentityEvidenceService
from ..messaging.send import (
    TRANSIENT_SKIP_REASONS,
    send_sms,
    send_sms_confirmation,
)
from ..messaging.sender import (
    RETRYABLE_PROVIDER_FAILURES,
    AcsSmsSender,
    MessageSender,
    twilio_sms_status_callback_url,
)
from ..models import OutreachJob
from ..pilot_controls import (
    JobPilotGate,
    job_gate_for_snapshot,
    mark_participant_contact_started,
    operational_switch_snapshot_from_environment,
)
from ..telemetry import configure_job_telemetry, emit_worker_summary
from .config import durable_booking_confirmation_enabled, durable_sms_enabled
from .effects import (
    claim_effects,
    lock_dispatching_effect,
    mark_canceled,
    mark_dispatching,
    mark_reconcile_required,
    mark_rejected,
    mark_retryable_failure,
    mark_succeeded,
)

SessionFactory = Callable[[], Session]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunOnceResult:
    """Aggregate-only outcome from one bounded worker invocation."""

    enabled: bool
    claimed: int = 0
    succeeded: int = 0
    rejected: int = 0
    canceled: int = 0
    retried: int = 0
    dead_lettered: int = 0
    handoffs_queued: int = 0
    reconcile_required: int = 0

    def as_summary(self) -> dict[str, int | bool]:
        """Return an aggregate-only representation suitable for Job logs."""
        return {
            "enabled": self.enabled,
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "rejected": self.rejected,
            "canceled": self.canceled,
            "retried": self.retried,
            "dead_lettered": self.dead_lettered,
            "handoffs_queued": self.handoffs_queued,
            "reconcile_required": self.reconcile_required,
        }


def run_once(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    worker_id: str,
    sender: MessageSender,
    programme_gate: JobPilotGate | None,
    now: datetime,
    enabled: bool = False,
    booking_confirmation_enabled: bool = False,
    lease_for: timedelta = timedelta(minutes=5),
    limit: int = 10,
    identity_service: IdentityEvidenceService | None = None,
) -> RunOnceResult:
    """Claim and dispatch a finite batch; disabled unless explicitly enabled."""
    if not enabled:
        return RunOnceResult(enabled=False)
    if programme_gate is None:
        return RunOnceResult(enabled=False)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)

    with session_factory() as session:
        claimed = claim_effects(
            session,
            clinic_id=clinic_id,
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
            limit=limit,
            effect_types=(ExternalEffectType.SMS,),
        )
        effect_ids = [effect.id for effect in claimed]
        session.commit()

    succeeded = 0
    rejected = 0
    canceled = 0
    retried = 0
    dead_lettered = 0
    handoffs_queued = 0
    reconcile_required = 0
    for effect_id in effect_ids:
        with session_factory() as session:
            effect = mark_dispatching(
                session,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
            )
            effect_type = effect.effect_type
            outreach_job_id = effect.aggregate_id
            effect_payload = (
                dict(effect.payload) if isinstance(effect.payload, dict) else {}
            )
            status_callback_url = twilio_sms_status_callback_url(effect.callback_token)
            session.commit()

        if effect_type != ExternalEffectType.SMS:
            with session_factory() as session:
                mark_reconcile_required(
                    session,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    worker_id=worker_id,
                    now=now,
                    reason_code="unsupported_effect_type",
                )
                session.commit()
            reconcile_required += 1
            continue

        try:
            with session_factory() as session:
                lock_dispatching_effect(
                    session,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    worker_id=worker_id,
                )
                with clinic_scope(session, clinic_id):
                    job = session.execute(
                        tenant_select(OutreachJob).where(
                            OutreachJob.id == outreach_job_id
                        )
                    ).scalar_one_or_none()
                    pilot_decision = (
                        programme_gate(session, clinic_id, job, now)
                        if job is not None
                        else None
                    )
                if job is None:
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code="outreach_context_missing",
                    )
                    session.commit()
                    canceled += 1
                    continue
                if pilot_decision is None:
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code="programme_gate_invalid",
                    )
                    session.commit()
                    canceled += 1
                    continue
                if not pilot_decision.allowed:
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code=pilot_decision.reason,
                    )
                    session.commit()
                    canceled += 1
                    continue
                confirmation_action_id = effect_payload.get("booking_action_id")
                confirmation_payload = (
                    isinstance(confirmation_action_id, str)
                    and bool(confirmation_action_id)
                    and effect_payload
                    == {
                        "intent": "booking_confirmation",
                        "outreach_job_id": outreach_job_id,
                        "booking_action_id": confirmation_action_id,
                    }
                )
                recall_payload = effect_payload == {
                    "intent": "recall",
                    "outreach_job_id": outreach_job_id,
                }
                if not confirmation_payload and not recall_payload:
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code="invalid_sms_effect_contract",
                    )
                    session.commit()
                    canceled += 1
                    continue
                if confirmation_payload and not booking_confirmation_enabled:
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code="booking_confirmation_disabled",
                    )
                    session.commit()
                    canceled += 1
                    continue
                if confirmation_payload and not is_booking_confirmation_effect_authorized(
                    session,
                    clinic_id=clinic_id,
                    effect=lock_dispatching_effect(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                    ),
                    booking_action_id=confirmation_action_id,
                    identity_service=identity_service,
                ):
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code="booking_confirmation_authority_invalid",
                    )
                    session.commit()
                    canceled += 1
                    continue
                outcome = "canceled"
                if confirmation_payload:
                    send_result = send_sms_confirmation(
                        session,
                        clinic_id,
                        outreach_job_id,
                        now,
                        sender,
                        pilot_gate=programme_gate,
                        booking_action_id=confirmation_action_id,
                        status_callback_url=status_callback_url,
                        identity_service=identity_service,
                    )
                else:
                    send_result = send_sms(
                        session,
                        clinic_id,
                        outreach_job_id,
                        now,
                        sender,
                        pilot_gate=programme_gate,
                        status_callback_url=status_callback_url,
                    )
                if send_result.sent and send_result.provider_message_id:
                    settled_effect = mark_succeeded(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        provider_resource_id=send_result.provider_message_id,
                    )
                    if pilot_decision.participant_id is not None:
                        mark_participant_contact_started(
                            session,
                            clinic_id=clinic_id,
                            participant_id=pilot_decision.participant_id,
                            now=now,
                        )
                    outcome = (
                        "succeeded"
                        if settled_effect.state == ExternalEffectState.SUCCEEDED
                        else "reconcile_required"
                    )
                elif send_result.sent or send_result.idempotent:
                    mark_reconcile_required(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code="missing_provider_evidence",
                    )
                    outcome = "reconcile_required"
                elif (
                    send_result.skip_reason is not None
                    and send_result.skip_reason in TRANSIENT_SKIP_REASONS
                ):
                    settled_effect, handoff_created = mark_retryable_failure(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code=send_result.skip_reason.value,
                        not_before=send_result.retry_at,
                        provider_dispatch_started=False,
                        failure_class="PreDispatchTransient",
                    )
                    outcome = (
                        "dead_lettered"
                        if settled_effect.state == ExternalEffectState.DEAD_LETTER
                        else "retried"
                    )
                    if handoff_created:
                        handoffs_queued += 1
                elif send_result.skip_reason is not None:
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code=send_result.skip_reason.value,
                    )
                    outcome = "canceled"
                elif send_result.error == "subject_frozen":
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code="subject_frozen",
                    )
                    outcome = "canceled"
                elif (
                    send_result.failure_code is not None
                    and send_result.failure_code in RETRYABLE_PROVIDER_FAILURES
                ):
                    settled_effect, handoff_created = mark_retryable_failure(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code=send_result.failure_code.value,
                    )
                    outcome = (
                        "dead_lettered"
                        if settled_effect.state == ExternalEffectState.DEAD_LETTER
                        else "retried"
                    )
                    if handoff_created:
                        handoffs_queued += 1
                elif send_result.error is not None:
                    mark_rejected(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                    )
                    outcome = "rejected"
                else:
                    mark_canceled(
                        session,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        worker_id=worker_id,
                        now=now,
                        reason_code="not_dispatched",
                    )
                    outcome = "canceled"
                session.commit()
            if outcome == "succeeded":
                succeeded += 1
            elif outcome == "rejected":
                rejected += 1
            elif outcome == "retried":
                retried += 1
            elif outcome == "dead_lettered":
                dead_lettered += 1
            elif outcome == "reconcile_required":
                reconcile_required += 1
            else:
                canceled += 1
        except Exception as exc:  # noqa: BLE001 - any post-dispatch uncertainty is quarantined
            logger.warning(
                "Durable SMS post-dispatch transition failed; quarantining effect "
                "exception_class=%s",
                exc.__class__.__name__,
                extra={
                    "effect_type": ExternalEffectType.SMS.value,
                },
            )
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

    result = RunOnceResult(
        enabled=True,
        claimed=len(effect_ids),
        succeeded=succeeded,
        rejected=rejected,
        canceled=canceled,
        retried=retried,
        dead_lettered=dead_lettered,
        handoffs_queued=handoffs_queued,
        reconcile_required=reconcile_required,
    )
    emit_worker_summary("sms_dispatch", result.as_summary())
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Run one finite durable SMS worker invocation."""
    parser = argparse.ArgumentParser(description="Run one durable Clinic Recall SMS batch.")
    parser.add_argument("--clinic-id", required=True, help="Internal clinic scope identifier.")
    parser.add_argument("--worker-id", default=None, help="Lease owner; defaults to execution host.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum effects to claim.")
    parser.add_argument(
        "--now",
        default=None,
        help="Timezone-aware ISO-8601 timestamp; defaults to current UTC time.",
    )
    args = parser.parse_args(argv)

    now = _parse_now(args.now)
    _bootstrap_runtime_configuration()
    if not durable_sms_enabled():
        print(json.dumps(RunOnceResult(enabled=False).as_summary(), sort_keys=True))
        return 0
    switches = operational_switch_snapshot_from_environment()
    if not switches.decision(Channel.SMS, now).allowed:
        print(json.dumps(RunOnceResult(enabled=False).as_summary(), sort_keys=True))
        return 0

    result = run_once(
        get_sessionmaker(),
        clinic_id=args.clinic_id,
        worker_id=args.worker_id or _default_worker_id(),
        sender=AcsSmsSender(),
        programme_gate=job_gate_for_snapshot(switches, Channel.SMS),
        now=now,
        enabled=True,
        booking_confirmation_enabled=durable_booking_confirmation_enabled(),
        limit=args.limit,
    )
    print(json.dumps(result.as_summary(), sort_keys=True))
    return 0


def _default_worker_id() -> str:
    value = os.getenv("CONTAINER_APP_JOB_EXECUTION_NAME") or socket.gethostname()
    return value.strip()[:128] or "clinic-recall-worker"


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed


def _bootstrap_runtime_configuration(
    bootstrap: Callable[[], bool] | None = None,
) -> None:
    """Hydrate cloud configuration before constructing provider or DB clients."""
    if os.getenv("AZURE_APPCONFIG_ENDPOINT", "").strip():
        if bootstrap is None:
            from apps.artagent.backend.config.appconfig_provider import bootstrap_appconfig

            bootstrap = bootstrap_appconfig
        if not bootstrap():
            raise RuntimeError("Azure App Configuration failed to load; worker stopped")
    configure_job_telemetry()


if __name__ == "__main__":
    raise SystemExit(main())