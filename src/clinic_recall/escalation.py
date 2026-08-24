"""Deterministic staff escalation service for Clinic Recall."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .enums import (
    AuditAction,
    Channel,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    InteractionDirection,
    InteractionIntent,
    InteractionOutcome,
    OutreachState,
)
from .handoffs import ensure_handoff_receipt
from .messaging.audit import audit_action
from .models import Escalation, Interaction, OutreachJob, Patient
from .rights import assert_patient_writable
from .telemetry import queue_after_commit


@dataclass(frozen=True)
class EscalationResult:
    """Outcome of routing a call to staff review."""

    escalation_id: str
    interaction_id: str
    reason: EscalationReason
    priority: EscalationPriority
    created: bool
    idempotent: bool
    upgraded: bool


_ACTIVE_STATUSES = {
    EscalationStatus.OPEN,
    EscalationStatus.ACKNOWLEDGED,
}
_REASON_RANK = {
    EscalationReason.AMBIGUOUS: 1,
    EscalationReason.FAILED_CONTACT: 2,
    EscalationReason.COMPLAINT: 3,
    EscalationReason.CLINICAL: 4,
    EscalationReason.URGENT: 5,
}


def escalate_to_staff(
    session: Session,
    clinic_id: str,
    *,
    patient_id: str,
    outreach_job_id: str,
    reason: EscalationReason | str,
    now: datetime,
    context: str = "",
) -> EscalationResult:
    """Get, create, or upgrade one active outreach escalation."""
    _require_aware("now", now)
    escalated_reason = _coerce_reason(reason)
    priority = _priority_for_reason(escalated_reason)
    with clinic_scope(session, clinic_id):
        patient = session.execute(
            tenant_select(Patient).where(Patient.id == patient_id)
        ).scalar_one_or_none()
        if patient is None:
            raise LookupError(f"patient {patient_id!r} not found for clinic")
        assert_patient_writable(session, clinic_id, patient.id)
        job = session.execute(
            tenant_select(OutreachJob).where(
                OutreachJob.id == outreach_job_id,
                OutreachJob.patient_id == patient_id,
            )
        ).scalar_one_or_none()
        if job is None:
            raise LookupError(f"outreach job {outreach_job_id!r} not found for patient")

        existing = _find_active_escalation(session, job.id)
        if existing is not None:
            result = _reuse_or_upgrade_escalation(
                session,
                existing,
                job=job,
                reason=escalated_reason,
                context=context,
            )
            ensure_handoff_receipt(session, clinic_id, existing, now=now)
            return result

        interaction = Interaction(
            id=f"interaction-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            outreach_job_id=job.id,
            channel=Channel.CALL,
            direction=InteractionDirection.INBOUND,
            content=context[:1000] or None,
            intent=_intent_for_reason(escalated_reason),
            outcome=InteractionOutcome.ROUTED_TO_STAFF,
            occurred_at=now,
        )
        escalation = Escalation(
            id=f"escalation-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            patient_id=patient.id,
            outreach_job_id=job.id,
            reason=escalated_reason,
            priority=priority,
            context_ref=interaction.id,
            status=EscalationStatus.OPEN,
        )
        try:
            with session.begin_nested():
                session.add(interaction)
                session.add(escalation)
                job.state = OutreachState.ESCALATED
                audit_action(
                    session,
                    clinic_id,
                    AuditAction.ESCALATE,
                    escalation.id,
                    {
                        "outreach_job_id": job.id,
                        "interaction_id": interaction.id,
                        "reason": escalated_reason.value,
                        "priority": priority.value,
                        "occurred_at": now,
                    },
                    actor="system:escalation",
                )
                session.flush()
                ensure_handoff_receipt(session, clinic_id, escalation, now=now)
        except IntegrityError:
            winner = _find_active_escalation(session, job.id)
            if winner is None:
                raise
            result = _reuse_or_upgrade_escalation(
                session,
                winner,
                job=job,
                reason=escalated_reason,
                context=context,
            )
            ensure_handoff_receipt(session, clinic_id, winner, now=now)
            return result
        queue_after_commit(
            session,
            "voice.escalation.triggered",
            {
                "channel": "call",
                "reason": escalated_reason.value,
                "priority": priority.value,
            },
        )
        return EscalationResult(
            escalation_id=escalation.id,
            interaction_id=interaction.id,
            reason=escalated_reason,
            priority=priority,
            created=True,
            idempotent=False,
            upgraded=False,
        )


def _find_active_escalation(
    session: Session,
    outreach_job_id: str,
) -> Escalation | None:
    return session.execute(
        tenant_select(Escalation)
        .where(
            Escalation.outreach_job_id == outreach_job_id,
            Escalation.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(Escalation.created_at, Escalation.id)
        .limit(1)
    ).scalars().first()


def _reuse_or_upgrade_escalation(
    session: Session,
    escalation: Escalation,
    *,
    job: OutreachJob,
    reason: EscalationReason,
    context: str,
) -> EscalationResult:
    upgraded = _REASON_RANK[reason] > _REASON_RANK[escalation.reason]
    if upgraded:
        escalation.reason = reason
        escalation.priority = _priority_for_reason(reason)
        if escalation.context_ref and context:
            interaction = session.execute(
                tenant_select(Interaction).where(
                    Interaction.id == escalation.context_ref
                )
            ).scalar_one_or_none()
            if interaction is not None:
                interaction.content = context[:1000]
                interaction.intent = _intent_for_reason(reason)
        session.flush()
    job.state = OutreachState.ESCALATED
    return EscalationResult(
        escalation_id=escalation.id,
        interaction_id=escalation.context_ref or "",
        reason=escalation.reason,
        priority=escalation.priority,
        created=False,
        idempotent=True,
        upgraded=upgraded,
    )


def _coerce_reason(reason: EscalationReason | str) -> EscalationReason:
    if isinstance(reason, EscalationReason):
        return reason
    try:
        return EscalationReason(reason)
    except ValueError as exc:
        raise ValueError(f"unsupported escalation reason: {reason!r}") from exc


def _priority_for_reason(reason: EscalationReason) -> EscalationPriority:
    if reason in {EscalationReason.CLINICAL, EscalationReason.URGENT, EscalationReason.COMPLAINT}:
        return EscalationPriority.HIGH
    return EscalationPriority.NORMAL


def _intent_for_reason(reason: EscalationReason) -> InteractionIntent:
    if reason == EscalationReason.CLINICAL:
        return InteractionIntent.CLINICAL
    if reason == EscalationReason.URGENT:
        return InteractionIntent.URGENT
    return InteractionIntent.UNCLEAR


def _require_aware(field: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")