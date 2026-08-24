"""Campaign cadence orchestration for Clinic Recall Phase 2."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..db import clinic_scope, tenant_select
from ..durable.effects import cancel_undispatched_effects
from ..durable.enqueue import enqueue_sms_effect
from ..enums import (
    AuditAction,
    CampaignStatus,
    Channel,
    ExternalEffectType,
    OutreachState,
)
from ..models import Campaign, OutreachJob
from ..pilot_controls import PatientPilotGate
from .audit import audit_action

EMAIL_POLICY_EXCLUDED = "patient_email_cadence_disabled"
STOP_STATES = {OutreachState.REPLIED, OutreachState.ESCALATED, OutreachState.COMPLETED}


@dataclass
class CadenceResult:
    """Counts from one deterministic cadence run."""

    sms_enqueued: int = 0
    sms_existing: int = 0
    sms_canceled: int = 0
    sms_sent: int = 0
    email_sent: int = 0
    email_policy_excluded: int = 0
    sms_no_reply: int = 0
    email_no_reply: int = 0
    skipped: Counter[str] = field(default_factory=Counter)

    def as_summary(self) -> dict[str, object]:
        return {
            "sms_enqueued": self.sms_enqueued,
            "sms_existing": self.sms_existing,
            "sms_canceled": self.sms_canceled,
            "sms_sent": self.sms_sent,
            "email_sent": self.email_sent,
            "email_policy_excluded": self.email_policy_excluded,
            "sms_no_reply": self.sms_no_reply,
            "email_no_reply": self.email_no_reply,
            "skipped": dict(self.skipped),
        }


def run_cadence(
    session: Session,
    clinic_id: str,
    now: datetime,
    *,
    sender: object | None = None,
    limit: int = 100,
    pilot_gate: PatientPilotGate,
) -> CadenceResult:
    """Plan one SMS-first cadence pass without invoking a provider."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (got naive datetime)")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    del sender

    result = CadenceResult()
    with clinic_scope(session, clinic_id):
        _exclude_queued_email_jobs(session, clinic_id, result, limit=limit)
        _plan_due_queued_jobs(
            session,
            clinic_id,
            now,
            result,
            pilot_gate=pilot_gate,
            limit=limit,
        )
        session.flush()
    return result


def _plan_due_queued_jobs(
    session: Session,
    clinic_id: str,
    now: datetime,
    result: CadenceResult,
    *,
    pilot_gate: PatientPilotGate,
    limit: int,
) -> None:
    active_campaign_ids = tenant_select(Campaign).with_only_columns(Campaign.id).where(
        Campaign.status == CampaignStatus.ACTIVE
    )
    jobs = list(
        session.execute(
            tenant_select(OutreachJob)
            .where(
                OutreachJob.channel == Channel.SMS,
                OutreachJob.state == OutreachState.QUEUED,
                (OutreachJob.next_action_at.is_(None) | (OutreachJob.next_action_at <= now)),
                OutreachJob.campaign_id.in_(active_campaign_ids),
            )
            .order_by(OutreachJob.created_at, OutreachJob.id)
            .limit(limit)
        ).scalars()
    )
    for job in jobs:
        if _thread_stopped(session, job):
            continue
        pilot_decision = pilot_gate(
            session,
            clinic_id,
            job.patient_id,
            Channel.SMS,
            now,
        )
        if not pilot_decision.allowed:
            result.skipped[pilot_decision.reason] += 1
            result.sms_canceled += cancel_undispatched_effects(
                session,
                clinic_id=clinic_id,
                aggregate_type="outreach_job",
                aggregate_id=job.id,
                effect_type=ExternalEffectType.SMS,
                now=now,
                reason_code=pilot_decision.reason,
            )
            audit_action(
                session,
                clinic_id,
                AuditAction.SKIP_CANDIDATE,
                job.id,
                {
                    "channel": Channel.SMS.value,
                    "skip_reason": pilot_decision.reason,
                },
                actor="system:cadence-planner",
            )
            continue
        _effect, created = enqueue_sms_effect(
            session,
            clinic_id=clinic_id,
            outreach_job_id=job.id,
            idempotency_key=f"cadence:sms:{job.id}",
            available_at=now,
        )
        if created:
            result.sms_enqueued += 1
        else:
            result.sms_existing += 1


def _exclude_queued_email_jobs(
    session: Session,
    clinic_id: str,
    result: CadenceResult,
    *,
    limit: int,
) -> None:
    jobs = list(
        session.execute(
            tenant_select(OutreachJob)
            .where(
                OutreachJob.channel == Channel.EMAIL,
                OutreachJob.state == OutreachState.QUEUED,
            )
            .order_by(OutreachJob.created_at, OutreachJob.id)
            .limit(limit)
        ).scalars()
    )
    for job in jobs:
        job.state = OutreachState.FAILED
        job.next_action_at = None
        result.email_policy_excluded += 1
        result.skipped[EMAIL_POLICY_EXCLUDED] += 1
        audit_action(
            session,
            clinic_id,
            AuditAction.SKIP_CANDIDATE,
            job.id,
            {"channel": Channel.EMAIL.value, "skip_reason": EMAIL_POLICY_EXCLUDED},
            actor="system:cadence-planner",
        )


def _thread_stopped(session: Session, job: OutreachJob) -> bool:
    rows = session.execute(
        tenant_select(OutreachJob).where(
            OutreachJob.campaign_id == job.campaign_id,
            OutreachJob.patient_id == job.patient_id,
            OutreachJob.appointment_id == job.appointment_id,
            OutreachJob.state.in_(STOP_STATES),
        )
    ).scalars()
    return any(row.id != job.id for row in rows)