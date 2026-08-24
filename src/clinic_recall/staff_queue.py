"""Staff approval and escalation queue for Clinic Recall Phase 4."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .durable.enqueue import enqueue_cliniko_booking_effect
from .enums import (
    AuditAction,
    BookingActionStatus,
    BookingActionType,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    OutreachState,
)
from .handoffs import acknowledge_handoff_owner, mark_handoff_resolved
from .identity_evidence import IdentityAction, IdentityEvidenceService
from .messaging.audit import audit_action
from .messaging.sender import MessageSender
from .models import (
    Appointment,
    AvailabilitySlot,
    BookingAction,
    Escalation,
    ExternalEffect,
    ExternalEffectHandoff,
    HandoffReceipt,
    Interaction,
    OutreachJob,
    Patient,
)
from .pilot_controls import JobPilotGate
from .rights import assert_patient_writable

HIGH_PRIORITY_REASONS = {
    EscalationReason.URGENT,
    EscalationReason.CLINICAL,
    EscalationReason.COMPLAINT,
}


class QueueDecision(StrEnum):
    """Staff decisions supported by the HITL queue."""

    APPROVE = "approve"
    REJECT = "reject"
    RESOLVE = "resolve"


class StaffQueueItem(BaseModel):
    """Patient-minimised queue item returned to staff surfaces."""

    model_config = ConfigDict(from_attributes=True)

    item_id: str
    kind: Literal["escalation", "booking_action", "external_effect_handoff"]
    priority: Literal["high", "normal", "low"]
    reason: str
    status: str
    patient_name: str
    outreach_state: str | None = None
    appointment_start: datetime | None = None
    slot_start: datetime | None = None
    booking_type: str | None = None
    context_summary: str
    created_at: datetime
    severity: str
    delivery_state: str
    queued_at: datetime
    due_at: datetime
    overdue: bool
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    alternate_requested: bool = False
    owner_resolved: bool = False


class QueueResolutionResult(BaseModel):
    """Outcome returned after a staff resolve action."""

    item_id: str
    decision: QueueDecision
    resolved: bool
    idempotent: bool = False
    booking_status: str | None = None
    escalation_status: str | None = None
    external_effect_handoff_status: str | None = None
    confirmation_sent: bool = False
    provider_confirmed: bool = False
    write_back_state: str | None = None
    error: str | None = None


class QueueAcknowledgementResult(BaseModel):
    """Outcome returned after a staff acknowledgement action."""

    item_id: str
    acknowledged: bool
    resolved: bool = False
    idempotent: bool = False
    booking_status: str | None = None
    escalation_status: str | None = None
    external_effect_handoff_status: str | None = None


def list_staff_queue(
    session: Session,
    clinic_id: str,
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> list[StaffQueueItem]:
    """List all current human work owners for one scoped clinic."""
    bounded_limit = max(1, min(limit, 250))
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    with clinic_scope(session, clinic_id):
        items = (
            _list_escalations(session, observed_at)
            + _list_pending_booking_actions(session, observed_at)
            + _list_external_effect_handoffs(session, observed_at)
        )
    return sorted(items, key=_queue_sort_key)[:bounded_limit]


def acknowledge_queue_item(
    session: Session,
    clinic_id: str,
    item_id: str,
    *,
    staff_actor: str,
    now: datetime,
) -> QueueAcknowledgementResult:
    """Acknowledge ownership without resolving or authorising queue work."""
    _require_aware("now", now)
    if not staff_actor.strip():
        raise ValueError("staff_actor is required")
    item_kind, row_id = _split_item_id(item_id)
    with clinic_scope(session, clinic_id):
        owner = _load_queue_owner(session, item_kind, row_id)
        if owner is None:
            raise LookupError("queue item not found for clinic")
        _receipt, transitioned = acknowledge_handoff_owner(
            session,
            clinic_id=clinic_id,
            owner=owner,
            actor=staff_actor,
            now=now,
        )
        return QueueAcknowledgementResult(
            item_id=f"{item_kind}:{owner.id}",
            acknowledged=True,
            idempotent=not transitioned,
            booking_status=(
                owner.status.value if isinstance(owner, BookingAction) else None
            ),
            escalation_status=(
                owner.status.value if isinstance(owner, Escalation) else None
            ),
            external_effect_handoff_status=(
                owner.status if isinstance(owner, ExternalEffectHandoff) else None
            ),
        )


def resolve_queue_item(
    session: Session,
    clinic_id: str,
    item_id: str,
    decision: QueueDecision | str,
    *,
    staff_actor: str,
    now: datetime,
    sender: MessageSender | None = None,
    pilot_gate: JobPilotGate,
    write_back_enabled: bool = False,
    reason: str | None = None,
    identity_service: IdentityEvidenceService | None = None,
) -> QueueResolutionResult:
    """Resolve a queue item under server-side clinic scope.

    `approve` is valid only for pending booking actions and completes the
    deterministic booking after re-checking consent, slot validity, and slot
    conflicts. `reject` never books. Escalations can be rejected/resolved only.
    """
    _require_aware("now", now)
    if not staff_actor.strip():
        raise ValueError("staff_actor is required")
    resolved_decision = QueueDecision(decision)
    item_kind, row_id = _split_item_id(item_id)
    with clinic_scope(session, clinic_id):
        owner = _load_queue_owner(session, item_kind, row_id)
        if owner is None:
            raise LookupError("queue item not found for clinic")
        receipt = _load_owner_receipt(session, item_kind, row_id)
        owner_open = (
            isinstance(owner, BookingAction)
            and owner.status == BookingActionStatus.PENDING
            or isinstance(owner, Escalation)
            and owner.status in {EscalationStatus.OPEN, EscalationStatus.ACKNOWLEDGED}
            or isinstance(owner, ExternalEffectHandoff)
            and owner.status in {"queued", "acknowledged"}
        )
        if owner_open:
            receipt, _transitioned = acknowledge_handoff_owner(
                session,
                clinic_id=clinic_id,
                owner=owner,
                actor=staff_actor,
                now=now,
            )
        if item_kind == "booking_action":
            result = _resolve_booking_action(
                session,
                clinic_id,
                row_id,
                resolved_decision,
                staff_actor=staff_actor,
                now=now,
                sender=sender,
                pilot_gate=pilot_gate,
                write_back_enabled=write_back_enabled,
                reason=reason,
                identity_service=identity_service,
            )
        elif item_kind == "external_effect_handoff":
            if resolved_decision != QueueDecision.RESOLVE:
                raise ValueError("external effect handoffs support resolve only")
            transitioned = owner.status != "resolved"
            if transitioned:
                owner.status = "resolved"
                audit_action(
                    session,
                    clinic_id,
                    AuditAction.RESOLVE,
                    owner.id,
                    {
                        "resolved_by": staff_actor,
                        "occurred_at": now,
                    },
                    actor=staff_actor,
                )
                session.flush()
            result = QueueResolutionResult(
                item_id=f"external_effect_handoff:{owner.id}",
                decision=resolved_decision,
                resolved=True,
                idempotent=not transitioned,
                external_effect_handoff_status=owner.status,
            )
        else:
            result = _resolve_escalation(
                session,
                clinic_id,
                row_id,
                resolved_decision,
                staff_actor=staff_actor,
                now=now,
                reason=reason,
            )
        if result.resolved:
            if receipt is None:
                raise ValueError("resolved queue item is missing its handoff receipt")
            mark_handoff_resolved(session, receipt, actor=staff_actor, now=now)
        return result


def _list_escalations(session: Session, now: datetime) -> list[StaffQueueItem]:
    rows = list(
        session.execute(
            tenant_select(Escalation)
            .where(Escalation.status != EscalationStatus.RESOLVED)
            .order_by(Escalation.created_at, Escalation.id)
        ).scalars()
    )
    items: list[StaffQueueItem] = []
    for escalation in rows:
        receipt = _load_owner_receipt(session, "escalation", escalation.id)
        if receipt is None:
            continue
        patient = _load_patient(session, escalation.patient_id)
        if patient is None:
            continue
        interaction = _load_interaction(session, escalation.context_ref)
        job = _load_job(session, interaction.outreach_job_id) if interaction else None
        appointment = _load_appointment(session, job.appointment_id) if job and job.appointment_id else None
        items.append(
            StaffQueueItem(
                item_id=f"escalation:{escalation.id}",
                kind="escalation",
                priority=_display_priority(escalation.priority, escalation.reason),
                reason=escalation.reason.value,
                status=escalation.status.value,
                patient_name=patient.name,
                outreach_state=job.state.value if job else None,
                appointment_start=_as_utc(appointment.start_at) if appointment else None,
                context_summary=_context_summary(interaction, escalation.reason.value),
                created_at=_as_utc(escalation.created_at),
                **_receipt_fields(receipt, now),
            )
        )
    return items


def _list_pending_booking_actions(session: Session, now: datetime) -> list[StaffQueueItem]:
    rows = list(
        session.execute(
            tenant_select(BookingAction)
            .where(BookingAction.status == BookingActionStatus.PENDING)
            .order_by(BookingAction.created_at, BookingAction.id)
        ).scalars()
    )
    items: list[StaffQueueItem] = []
    for action in rows:
        receipt = _load_owner_receipt(session, "booking_action", action.id)
        if receipt is None:
            continue
        appointment = _load_appointment(session, action.appointment_id)
        patient = _load_patient(session, appointment.patient_id) if appointment else None
        if appointment is None or patient is None:
            continue
        slot = _load_slot(session, action.availability_slot_id)
        job = _load_job(session, action.outreach_job_id) if action.outreach_job_id else None
        items.append(
            StaffQueueItem(
                item_id=f"booking_action:{action.id}",
                kind="booking_action",
                priority="normal",
                reason="pending_booking_approval",
                status=action.status.value,
                patient_name=patient.name,
                outreach_state=job.state.value if job else None,
                appointment_start=_as_utc(appointment.start_at),
                slot_start=_as_utc(slot.start_at) if slot else None,
                booking_type=action.type.value,
                context_summary=(
                    f"pending {action.type.value} approval"
                    + (f" from {job.state.value}" if job else "")
                ),
                created_at=_as_utc(action.created_at),
                **_receipt_fields(receipt, now),
            )
        )
    return items


def _list_external_effect_handoffs(
    session: Session,
    now: datetime,
) -> list[StaffQueueItem]:
    rows = list(
        session.execute(
            tenant_select(ExternalEffectHandoff)
            .where(ExternalEffectHandoff.status != "resolved")
            .order_by(
                ExternalEffectHandoff.created_at,
                ExternalEffectHandoff.id,
            )
        ).scalars()
    )
    items: list[StaffQueueItem] = []
    for handoff in rows:
        receipt = _load_owner_receipt(
            session,
            "external_effect_handoff",
            handoff.id,
        )
        if receipt is None:
            continue
        effect = session.execute(
            tenant_select(ExternalEffect).where(
                ExternalEffect.id == handoff.external_effect_id
            )
        ).scalar_one_or_none()
        if effect is None:
            continue
        items.append(
            StaffQueueItem(
                item_id=f"external_effect_handoff:{handoff.id}",
                kind="external_effect_handoff",
                priority="normal",
                reason=handoff.reason_code,
                status=handoff.status,
                patient_name="Operational handoff",
                context_summary=(
                    f"{effect.effect_type.value} operation requires staff review"
                ),
                created_at=_as_utc(handoff.created_at),
                **_receipt_fields(receipt, now),
            )
        )
    return items


def _resolve_booking_action(
    session: Session,
    clinic_id: str,
    booking_action_id: str,
    decision: QueueDecision,
    *,
    staff_actor: str,
    now: datetime,
    sender: MessageSender | None,
    pilot_gate: JobPilotGate,
    write_back_enabled: bool,
    reason: str | None,
    identity_service: IdentityEvidenceService | None,
) -> QueueResolutionResult:
    action = _load_booking_action(session, booking_action_id)
    if action is None:
        raise LookupError("queue item not found for clinic")
    item_id = f"booking_action:{action.id}"

    if decision == QueueDecision.APPROVE:
        return _approve_booking_action(
            session,
            clinic_id,
            action,
            item_id,
            staff_actor,
            now,
            sender,
            pilot_gate,
            write_back_enabled,
            identity_service,
        )
    if decision in {QueueDecision.REJECT, QueueDecision.RESOLVE}:
        return _reject_booking_action(session, clinic_id, action, item_id, decision, staff_actor, now, reason)
    raise ValueError(f"unsupported queue decision: {decision.value}")


def _approve_booking_action(
    session: Session,
    clinic_id: str,
    action: BookingAction,
    item_id: str,
    staff_actor: str,
    now: datetime,
    sender: MessageSender | None,
    pilot_gate: JobPilotGate,
    write_back_enabled: bool,
    identity_service: IdentityEvidenceService | None,
) -> QueueResolutionResult:
    if action.status == BookingActionStatus.REJECTED:
        raise ValueError("booking action has already been rejected")
    transitioned = False
    if action.status == BookingActionStatus.PENDING:
        job, patient, appointment, slot = _validate_pending_action_for_approval(session, action, now)
        identity_action = (
            IdentityAction.BOOK_SLOT
            if action.type == BookingActionType.BOOK
            else IdentityAction.RESCHEDULE
        )
        if identity_service is None or not identity_service.authorize_bound_action(
            session,
            clinic_id=clinic_id,
            evidence_id=action.identity_evidence_id,
            evidence_policy_version=action.identity_policy_version,
            evidence_revision=action.identity_evidence_revision,
            patient_id=patient.id,
            channel=job.channel,
            action=identity_action,
        ).allowed:
            return QueueResolutionResult(
                item_id=item_id,
                decision=QueueDecision.APPROVE,
                resolved=False,
                booking_status=action.status.value,
                error="identity_t2_required",
            )
        other = _other_active_action_for_slot(session, action.id, slot.id)
        if other is not None:
            return QueueResolutionResult(
                item_id=item_id,
                decision=QueueDecision.APPROVE,
                resolved=False,
                booking_status=action.status.value,
                error="slot_already_booked",
            )
        action.status = BookingActionStatus.COMPLETED
        action.approved_by = staff_actor
        job.state = OutreachState.COMPLETED
        if write_back_enabled and action.type == BookingActionType.BOOK:
            enqueue_cliniko_booking_effect(
                session,
                clinic_id=clinic_id,
                booking_action_id=action.id,
                intent="create",
                available_at=now,
            )
        audit_action(
            session,
            clinic_id,
            AuditAction.APPROVE,
            action.id,
            {
                "outreach_job_id": job.id,
                "appointment_id": appointment.id,
                "patient_id": patient.id,
                "slot_id": slot.id,
                "approved_by": staff_actor,
                "occurred_at": now,
            },
            actor=staff_actor,
        )
        session.flush()
        transitioned = True

    return QueueResolutionResult(
        item_id=item_id,
        decision=QueueDecision.APPROVE,
        resolved=True,
        idempotent=not transitioned,
        booking_status=action.status.value,
        confirmation_sent=False,
        provider_confirmed=False,
        write_back_state=action.write_back_state.value,
    )


def _reject_booking_action(
    session: Session,
    clinic_id: str,
    action: BookingAction,
    item_id: str,
    decision: QueueDecision,
    staff_actor: str,
    now: datetime,
    reason: str | None,
) -> QueueResolutionResult:
    if action.status == BookingActionStatus.COMPLETED:
        raise ValueError("booking action has already been completed")
    transitioned = False
    if action.status == BookingActionStatus.PENDING:
        action.status = BookingActionStatus.REJECTED
        if action.outreach_job_id:
            job = _load_job(session, action.outreach_job_id)
            if job is not None:
                job.state = OutreachState.COMPLETED
        audit_action(
            session,
            clinic_id,
            AuditAction.REJECT if decision == QueueDecision.REJECT else AuditAction.RESOLVE,
            action.id,
            {
                "reason": (reason or "")[:250],
                "rejected_by": staff_actor,
                "occurred_at": now,
            },
            actor=staff_actor,
        )
        session.flush()
        transitioned = True
    return QueueResolutionResult(
        item_id=item_id,
        decision=decision,
        resolved=True,
        idempotent=not transitioned,
        booking_status=action.status.value,
    )


def _resolve_escalation(
    session: Session,
    clinic_id: str,
    escalation_id: str,
    decision: QueueDecision,
    *,
    staff_actor: str,
    now: datetime,
    reason: str | None,
) -> QueueResolutionResult:
    if decision == QueueDecision.APPROVE:
        raise ValueError("escalations cannot be approved into a booking")
    escalation = _load_escalation(session, escalation_id)
    if escalation is None:
        raise LookupError("queue item not found for clinic")
    item_id = f"escalation:{escalation.id}"
    transitioned = False
    if escalation.status != EscalationStatus.RESOLVED:
        escalation.status = EscalationStatus.RESOLVED
        escalation.assigned_to = staff_actor
        interaction = _load_interaction(session, escalation.context_ref)
        if interaction:
            job = _load_job(session, interaction.outreach_job_id)
            if job is not None:
                job.state = OutreachState.COMPLETED
        audit_action(
            session,
            clinic_id,
            AuditAction.REJECT if decision == QueueDecision.REJECT else AuditAction.RESOLVE,
            escalation.id,
            {
                "reason": (reason or "")[:250],
                "resolved_by": staff_actor,
                "occurred_at": now,
            },
            actor=staff_actor,
        )
        session.flush()
        transitioned = True
    return QueueResolutionResult(
        item_id=item_id,
        decision=decision,
        resolved=True,
        idempotent=not transitioned,
        escalation_status=escalation.status.value,
    )


def _validate_pending_action_for_approval(
    session: Session, action: BookingAction, now: datetime
) -> tuple[OutreachJob, Patient, Appointment, AvailabilitySlot]:
    if action.outreach_job_id is None:
        raise ValueError("booking action is missing outreach job")
    if action.availability_slot_id is None:
        raise ValueError("booking action is missing availability slot")
    job = _load_job(session, action.outreach_job_id)
    appointment = _load_appointment(session, action.appointment_id)
    slot = _load_slot(session, action.availability_slot_id)
    if job is None or appointment is None or slot is None:
        raise LookupError("booking action references missing scoped rows")
    patient = _load_patient(session, appointment.patient_id)
    if patient is None or job.patient_id != patient.id or job.appointment_id != appointment.id:
        raise LookupError("booking action references mismatched scoped rows")
    assert_patient_writable(session, action.clinic_id, patient.id)
    if patient.opt_out_flags.get("call") is True:
        raise ValueError("patient_opted_out")
    if patient.consent_flags.get("call") is not True:
        raise ValueError("no_call_consent")
    slot_start = _as_utc(slot.start_at)
    slot_end = _as_utc(slot.end_at)
    if slot_start <= now or slot_end <= slot_start:
        raise ValueError("slot_unavailable")
    return job, patient, appointment, slot


def _load_patient(session: Session, patient_id: str | None) -> Patient | None:
    if not patient_id:
        return None
    return session.execute(tenant_select(Patient).where(Patient.id == patient_id)).scalar_one_or_none()


def _load_appointment(session: Session, appointment_id: str | None) -> Appointment | None:
    if not appointment_id:
        return None
    return session.execute(
        tenant_select(Appointment).where(Appointment.id == appointment_id)
    ).scalar_one_or_none()


def _load_job(session: Session, outreach_job_id: str | None) -> OutreachJob | None:
    if not outreach_job_id:
        return None
    return session.execute(
        tenant_select(OutreachJob).where(OutreachJob.id == outreach_job_id)
    ).scalar_one_or_none()


def _load_slot(session: Session, slot_id: str | None) -> AvailabilitySlot | None:
    if not slot_id:
        return None
    return session.execute(
        tenant_select(AvailabilitySlot).where(AvailabilitySlot.id == slot_id)
    ).scalar_one_or_none()


def _load_interaction(session: Session, interaction_id: str | None) -> Interaction | None:
    if not interaction_id:
        return None
    return session.execute(
        tenant_select(Interaction).where(Interaction.id == interaction_id)
    ).scalar_one_or_none()


def _load_booking_action(session: Session, booking_action_id: str) -> BookingAction | None:
    return session.execute(
        tenant_select(BookingAction).where(BookingAction.id == booking_action_id)
    ).scalar_one_or_none()


def _load_escalation(session: Session, escalation_id: str) -> Escalation | None:
    return session.execute(
        tenant_select(Escalation).where(Escalation.id == escalation_id)
    ).scalar_one_or_none()


def _other_active_action_for_slot(
    session: Session, booking_action_id: str, slot_id: str
) -> BookingAction | None:
    return session.execute(
        tenant_select(BookingAction)
        .where(
            BookingAction.id != booking_action_id,
            BookingAction.availability_slot_id == slot_id,
            BookingAction.status != BookingActionStatus.REJECTED,
        )
        .limit(1)
    ).scalar_one_or_none()


def _load_queue_owner(
    session: Session,
    item_kind: str,
    row_id: str,
) -> BookingAction | Escalation | ExternalEffectHandoff | None:
    if item_kind == "booking_action":
        return _load_booking_action(session, row_id)
    if item_kind == "external_effect_handoff":
        return session.execute(
            tenant_select(ExternalEffectHandoff).where(
                ExternalEffectHandoff.id == row_id
            )
        ).scalar_one_or_none()
    return _load_escalation(session, row_id)


def _load_owner_receipt(
    session: Session,
    item_kind: str,
    row_id: str,
) -> HandoffReceipt | None:
    column = {
        "booking_action": HandoffReceipt.booking_action_id,
        "escalation": HandoffReceipt.escalation_id,
        "external_effect_handoff": HandoffReceipt.external_effect_handoff_id,
    }[item_kind]
    return session.execute(
        tenant_select(HandoffReceipt).where(column == row_id)
    ).scalar_one_or_none()


def _receipt_fields(
    receipt: HandoffReceipt,
    now: datetime,
) -> dict[str, object]:
    due_at = _as_utc(receipt.due_at)
    return {
        "severity": receipt.severity.value,
        "delivery_state": receipt.delivery_state.value,
        "queued_at": _as_utc(receipt.queued_at),
        "due_at": due_at,
        "overdue": (
            receipt.acknowledged_at is None
            and receipt.resolved_at is None
            and due_at <= now
        ),
        "acknowledged_at": (
            _as_utc(receipt.acknowledged_at)
            if receipt.acknowledged_at is not None
            else None
        ),
        "acknowledged_by": receipt.acknowledged_by,
        "alternate_requested": (
            receipt.alternate_state.value == "requested"
        ),
        "owner_resolved": receipt.resolved_at is not None,
    }


def _split_item_id(
    item_id: str,
) -> tuple[
    Literal["escalation", "booking_action", "external_effect_handoff"],
    str,
]:
    prefix, separator, row_id = item_id.partition(":")
    if separator != ":" or not row_id:
        raise ValueError("queue item id must be '<kind>:<id>'")
    if prefix not in {"escalation", "booking_action", "external_effect_handoff"}:
        raise ValueError("unsupported queue item kind")
    return prefix, row_id  # type: ignore[return-value]


def _display_priority(
    priority: EscalationPriority, reason: EscalationReason
) -> Literal["high", "normal", "low"]:
    if priority == EscalationPriority.HIGH or reason in HIGH_PRIORITY_REASONS:
        return "high"
    if priority == EscalationPriority.LOW:
        return "low"
    return "normal"


def _queue_sort_key(item: StaffQueueItem) -> tuple[int, datetime, str]:
    rank = 0 if item.priority == "high" else 1 if item.kind == "booking_action" else 2
    return (rank, item.created_at, item.item_id)


def _context_summary(interaction: Interaction | None, fallback_reason: str) -> str:
    if interaction is None:
        return fallback_reason
    parts = [
        interaction.channel.value,
        interaction.direction.value,
    ]
    if interaction.outcome:
        parts.append(interaction.outcome.value)
    if interaction.intent:
        parts.append(interaction.intent.value)
    return " ".join(parts)


def _require_aware(field: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)