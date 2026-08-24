"""Provider-free SMS-to-voice fallback planning."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from .candidate_queue import _patient_view, clinic_config_from_row
from .db import clinic_scope, tenant_select
from .durable.effects import cancel_undispatched_effects
from .durable.enqueue import enqueue_call_effect
from .eligibility import evaluate
from .enums import (
    ACTIVE_ESCALATION_STATUSES,
    AuditAction,
    BookingActionStatus,
    CampaignStatus,
    Channel,
    ExternalEffectState,
    ExternalEffectType,
    InteractionDirection,
    OutreachState,
    SkipReason,
)
from .messaging.audit import audit_action
from .messaging.history import contact_history_for_send
from .models import (
    BookingAction,
    Campaign,
    Clinic,
    Escalation,
    ExternalEffect,
    Interaction,
    OutreachJob,
    Patient,
)
from .pilot_controls import PilotGateDecision, pilot_gate_decision

VOICE_FALLBACK_DELAY = timedelta(hours=48)
TERMINAL_SMS_PROVIDER_STATUSES = frozenset({"delivery_succeeded", "delivery_failed"})
VOICE_STOP_STATES = frozenset(
    {OutreachState.REPLIED, OutreachState.ESCALATED, OutreachState.COMPLETED}
)


@dataclass
class VoiceCadenceResult:
    """Counts from one deterministic voice planning pass."""

    calls_enqueued: int = 0
    call_existing: int = 0
    calls_canceled: int = 0
    calls_initiated: int = 0
    idempotent_skips: int = 0
    failed_calls: int = 0
    skipped: Counter[str] = field(default_factory=Counter)

    def as_summary(self) -> dict[str, object]:
        return {
            "calls_enqueued": self.calls_enqueued,
            "call_existing": self.call_existing,
            "calls_canceled": self.calls_canceled,
            "calls_initiated": self.calls_initiated,
            "idempotent_skips": self.idempotent_skips,
            "failed_calls": self.failed_calls,
            "skipped": dict(self.skipped),
        }


ProgrammeGate = Callable[
    [Session, str, OutreachJob, datetime],
    PilotGateDecision | bool,
]


def run_voice_cadence(
    session: Session,
    clinic_id: str,
    now: datetime,
    *,
    initiator: object | None = None,
    programme_gate: ProgrammeGate | None = None,
    limit: int = 100,
) -> VoiceCadenceResult:
    """Plan eligible voice fallbacks without invoking a provider."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (got naive datetime)")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    del initiator

    result = VoiceCadenceResult()
    with clinic_scope(session, clinic_id):
        clinic = session.get(Clinic, clinic_id)
        if clinic is None:
            raise LookupError(f"clinic {clinic_id!r} not found")
        due_before = now.astimezone(UTC) - VOICE_FALLBACK_DELAY
        terminal_sms = sa.or_(
            sa.and_(
                ExternalEffect.state == ExternalEffectState.SUCCEEDED,
                ExternalEffect.provider_status.in_(TERMINAL_SMS_PROVIDER_STATUSES),
            ),
            sa.and_(
                ExternalEffect.state == ExternalEffectState.REJECTED,
                ExternalEffect.provider_status == "rejected",
            ),
        )
        due_sms_filter = (
            ExternalEffect.clinic_id == clinic_id,
            ExternalEffect.aggregate_type == "outreach_job",
            ExternalEffect.aggregate_id == OutreachJob.id,
            ExternalEffect.effect_type == ExternalEffectType.SMS,
            ExternalEffect.dispatch_started_at.is_not(None),
            ExternalEffect.dispatch_started_at <= due_before,
            terminal_sms,
        )
        due_sms_exists = (
            sa.select(ExternalEffect.id).where(*due_sms_filter).correlate(OutreachJob).exists()
        )
        pending_call_exists = (
            sa.select(ExternalEffect.id)
            .where(
                ExternalEffect.clinic_id == clinic_id,
                ExternalEffect.aggregate_type == "outreach_job",
                ExternalEffect.aggregate_id == OutreachJob.id,
                ExternalEffect.effect_type == ExternalEffectType.CALL,
                ExternalEffect.state.in_({ExternalEffectState.PENDING, ExternalEffectState.LEASED}),
            )
            .correlate(OutreachJob)
            .exists()
        )
        latest_due_dispatch = (
            sa.select(sa.func.max(ExternalEffect.dispatch_started_at))
            .where(*due_sms_filter)
            .correlate(OutreachJob)
            .scalar_subquery()
        )
        jobs = list(
            session.execute(
                tenant_select(OutreachJob)
                .where(OutreachJob.channel == Channel.SMS)
                .order_by(
                    sa.case(
                        (pending_call_exists, 0),
                        (due_sms_exists, 1),
                        else_=2,
                    ),
                    latest_due_dispatch.desc(),
                    OutreachJob.created_at,
                    OutreachJob.id,
                )
                .limit(limit)
            ).scalars()
        )
        for job in jobs:
            _plan_voice_fallback(
                session,
                clinic,
                job,
                now,
                programme_gate,
                result,
            )
        session.flush()
    return result


def _plan_voice_fallback(
    session: Session,
    clinic: Clinic,
    job: OutreachJob,
    now: datetime,
    programme_gate: ProgrammeGate | None,
    result: VoiceCadenceResult,
) -> None:
    stop_reason = _voice_stop_reason(session, job)
    if stop_reason is not None:
        _block_voice_fallback(session, clinic.id, job, now, stop_reason, result)
        return
    programme_decision = (
        pilot_gate_decision(programme_gate(session, clinic.id, job, now))
        if programme_gate is not None
        else PilotGateDecision(False, "programme_gate_unbound")
    )
    if not programme_decision.allowed:
        _block_voice_fallback(
            session,
            clinic.id,
            job,
            now,
            programme_decision.reason,
            result,
        )
        return

    sms_effects = list(
        session.execute(
            tenant_select(ExternalEffect).where(
                ExternalEffect.aggregate_type == "outreach_job",
                ExternalEffect.aggregate_id == job.id,
                ExternalEffect.effect_type == ExternalEffectType.SMS,
            )
        ).scalars()
    )
    if len(sms_effects) != 1:
        _block_voice_fallback(
            session,
            clinic.id,
            job,
            now,
            "sms_effect_missing_or_ambiguous",
            result,
        )
        return
    sms_effect = sms_effects[0]
    if sms_effect.dispatch_started_at is None:
        _block_voice_fallback(
            session,
            clinic.id,
            job,
            now,
            "sms_dispatch_not_started",
            result,
        )
        return
    dispatch_started_at = _as_utc(sms_effect.dispatch_started_at)
    if dispatch_started_at + VOICE_FALLBACK_DELAY > now.astimezone(UTC):
        _block_voice_fallback(session, clinic.id, job, now, "sms_wait_48h", result)
        return
    if not _sms_has_terminal_evidence(sms_effect):
        _block_voice_fallback(session, clinic.id, job, now, "sms_not_terminal", result)
        return

    patient = session.execute(
        tenant_select(Patient).where(Patient.id == job.patient_id)
    ).scalar_one_or_none()
    if patient is None:
        raise LookupError(f"patient {job.patient_id!r} not found for job")

    config = clinic_config_from_row(clinic)
    history = contact_history_for_send(session, clinic.id, patient.id, now, config)
    decision = evaluate(_patient_view(patient), config, history, now, Channel.CALL)
    if not decision.eligible:
        assert decision.skip_reason is not None  # nosec B101
        _block_voice_fallback(
            session,
            clinic.id,
            job,
            now,
            decision.skip_reason.value,
            result,
            audit=True,
        )
        return

    if not patient.phone:
        _block_voice_fallback(
            session,
            clinic.id,
            job,
            now,
            SkipReason.NOT_CONTACTABLE.value,
            result,
            audit=True,
        )
        return

    _effect, created = enqueue_call_effect(
        session,
        clinic_id=clinic.id,
        outreach_job_id=job.id,
        idempotency_key=f"cadence:call:{job.id}",
        available_at=now,
    )
    if created:
        result.calls_enqueued += 1
    else:
        result.call_existing += 1


def _voice_stop_reason(session: Session, job: OutreachJob) -> str | None:
    campaign_active = session.execute(
        tenant_select(Campaign)
        .with_only_columns(Campaign.id)
        .where(Campaign.id == job.campaign_id, Campaign.status == CampaignStatus.ACTIVE)
    ).first()
    if campaign_active is None:
        return "campaign_not_active"
    if _has_outbound_call(session, job.clinic_id, job.id):
        return "outbound_call_exists"
    completed_booking = session.execute(
        tenant_select(BookingAction)
        .with_only_columns(BookingAction.id)
        .where(
            BookingAction.outreach_job_id == job.id,
            BookingAction.status == BookingActionStatus.COMPLETED,
        )
    ).first()
    if completed_booking is not None:
        return "booking_completed"
    active_escalation = session.execute(
        tenant_select(Escalation)
        .with_only_columns(Escalation.id)
        .where(
            Escalation.outreach_job_id == job.id,
            Escalation.status.in_(ACTIVE_ESCALATION_STATUSES),
        )
    ).first()
    if active_escalation is not None:
        return "active_escalation"
    if job.state in VOICE_STOP_STATES:
        return f"outreach_{job.state.value}"
    related_stop = session.execute(
        tenant_select(OutreachJob).where(
            OutreachJob.campaign_id == job.campaign_id,
            OutreachJob.patient_id == job.patient_id,
            OutreachJob.appointment_id == job.appointment_id,
            OutreachJob.state.in_(VOICE_STOP_STATES),
        )
    ).first()
    if related_stop is not None:
        return "outreach_thread_stopped"
    inbound_reply = session.execute(
        tenant_select(Interaction)
        .with_only_columns(Interaction.id)
        .where(
            Interaction.clinic_id == job.clinic_id,
            Interaction.outreach_job_id == job.id,
            Interaction.direction == InteractionDirection.INBOUND,
        )
    ).first()
    return "inbound_reply" if inbound_reply is not None else None


def _sms_has_terminal_evidence(effect: ExternalEffect) -> bool:
    if (
        effect.state == ExternalEffectState.SUCCEEDED
        and effect.provider_status in TERMINAL_SMS_PROVIDER_STATUSES
    ):
        return True
    return effect.state == ExternalEffectState.REJECTED and effect.provider_status == "rejected"


def _audit_voice_skip(
    session: Session,
    clinic_id: str,
    job: OutreachJob,
    reason_code: str,
) -> None:
    audit_action(
        session,
        clinic_id,
        AuditAction.SKIP_CANDIDATE,
        job.id,
        {"channel": Channel.CALL.value, "skip_reason": reason_code},
        actor="system:voice-planner",
    )


def _block_voice_fallback(
    session: Session,
    clinic_id: str,
    job: OutreachJob,
    now: datetime,
    reason_code: str,
    result: VoiceCadenceResult,
    *,
    audit: bool = False,
) -> None:
    result.skipped[reason_code] += 1
    result.calls_canceled += cancel_undispatched_effects(
        session,
        clinic_id=clinic_id,
        aggregate_type="outreach_job",
        aggregate_id=job.id,
        effect_type=ExternalEffectType.CALL,
        now=now,
        reason_code=reason_code,
    )
    if audit:
        _audit_voice_skip(session, clinic_id, job, reason_code)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _has_outbound_call(
    session: Session,
    clinic_id: str,
    outreach_job_id: str,
) -> bool:
    return (
        session.execute(
            tenant_select(Interaction)
            .with_only_columns(Interaction.id)
            .where(
                Interaction.clinic_id == clinic_id,
                Interaction.outreach_job_id == outreach_job_id,
                Interaction.channel == Channel.CALL,
                Interaction.direction == InteractionDirection.OUTBOUND,
            )
        ).first()
        is not None
    )
