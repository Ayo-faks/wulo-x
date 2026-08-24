"""Deterministic booking actions for Clinic Recall voice rebooking."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .booking_confirmation import is_provider_booking_verified
from .booking_identity import canonical_booking_request_hash
from .db import clinic_scope, tenant_select
from .durable.enqueue import enqueue_cliniko_booking_effect
from .enums import (
    AppointmentStatus,
    AuditAction,
    BookingActionStatus,
    BookingActionType,
    BookingWriteBackState,
    IdentityEvidenceReason,
    IdentityTier,
    OutreachState,
)
from .handoffs import ensure_handoff_receipt
from .identity_evidence import (
    IdentityAction,
    IdentityAuthorizationContext,
    IdentityDecision,
    IdentityEvidenceService,
)
from .messaging.audit import audit_action
from .models import Appointment, AvailabilitySlot, BookingAction, OutreachJob, Patient
from .rights import SubjectFrozenError, assert_patient_writable
from .telemetry import queue_after_commit


@dataclass(frozen=True)
class BookingResult:
    """Outcome of a deterministic booking or reschedule attempt."""

    success: bool
    booking_action_id: str | None = None
    status: BookingActionStatus | None = None
    idempotent: bool = False
    queued_for_staff: bool = False
    staff_handoff_created: bool = False
    local_action_recorded: bool = False
    write_back_state: BookingWriteBackState | None = None
    provider_confirmed: bool = False
    error: str | None = None
    message: str | None = None


def book_slot(
    session: Session,
    clinic_id: str,
    *,
    patient_id: str,
    outreach_job_id: str,
    slot_id: str,
    now: datetime,
    require_staff_approval: bool = False,
    write_back_enabled: bool = False,
    identity_service: IdentityEvidenceService | None = None,
    identity_context: IdentityAuthorizationContext | None = None,
) -> BookingResult:
    """Book a valid slot for the outreach job's appointment, idempotently."""
    return _create_booking_action(
        session,
        clinic_id,
        patient_id=patient_id,
        outreach_job_id=outreach_job_id,
        slot_id=slot_id,
        now=now,
        action_type=BookingActionType.BOOK,
        appointment_id=None,
        require_staff_approval=require_staff_approval,
        write_back_enabled=write_back_enabled,
        identity_service=identity_service,
        identity_context=identity_context,
    )


def reschedule(
    session: Session,
    clinic_id: str,
    *,
    patient_id: str,
    outreach_job_id: str,
    appointment_id: str,
    slot_id: str,
    now: datetime,
    require_staff_approval: bool = False,
    write_back_enabled: bool = False,
    identity_service: IdentityEvidenceService | None = None,
    identity_context: IdentityAuthorizationContext | None = None,
) -> BookingResult:
    """Reschedule a patient-owned appointment into a valid slot, idempotently."""
    return _create_booking_action(
        session,
        clinic_id,
        patient_id=patient_id,
        outreach_job_id=outreach_job_id,
        slot_id=slot_id,
        now=now,
        action_type=BookingActionType.RESCHEDULE,
        appointment_id=appointment_id,
        require_staff_approval=require_staff_approval,
        write_back_enabled=write_back_enabled,
        identity_service=identity_service,
        identity_context=identity_context,
    )


def book_inbound_slot(
    session: Session,
    clinic_id: str,
    *,
    patient_id: str,
    appointment_id: str | None = None,
    slot_id: str,
    now: datetime,
    action_type: str | BookingActionType = BookingActionType.RESCHEDULE,
    require_staff_approval: bool = False,
    identity_service: IdentityEvidenceService | None = None,
    identity_context: IdentityAuthorizationContext | None = None,
) -> BookingResult:
    """Book or reschedule a known inbound SMS patient into a valid slot."""
    _require_aware("now", now)
    action = BookingActionType(action_type)
    with clinic_scope(session, clinic_id):
        patient = _load_patient(session, patient_id)
        if patient is None:
            raise LookupError(f"patient {patient_id!r} not found for clinic")
        try:
            assert_patient_writable(session, clinic_id, patient_id)
        except SubjectFrozenError:
            return BookingResult(False, error="subject_frozen")
        if patient.opt_out_flags.get("sms") is True:
            return BookingResult(False, error="patient_sms_opted_out")
        if patient.consent_flags.get("sms") is not True:
            return BookingResult(False, error="no_sms_consent")
        identity_decision = _authorize_booking(
            session,
            clinic_id=clinic_id,
            patient_id=patient_id,
            action=(
                IdentityAction.BOOK_SLOT
                if action == BookingActionType.BOOK
                else IdentityAction.RESCHEDULE
            ),
            identity_service=identity_service,
            identity_context=identity_context,
        )
        if not identity_decision.allowed:
            return BookingResult(False, error="identity_t2_required")

        slot = _load_slot(session, slot_id, for_update=True)
        if slot is None:
            raise LookupError(f"availability slot {slot_id!r} not found for clinic")
        if not _slot_is_fresh(slot, now):
            return BookingResult(False, error="slot_stale")
        slot_start = _as_utc(slot.start_at)
        slot_end = _as_utc(slot.end_at)
        if slot_start <= now or slot_end <= slot_start:
            return BookingResult(False, error="slot_unavailable")

        request_hash = canonical_booking_request_hash(
            clinic_id=clinic_id,
            patient_id=patient_id,
            appointment_id=appointment_id,
            slot=slot,
            action_type=action,
            outreach_job_id=None,
        )
        existing = _existing_action_for_slot(session, slot.id)
        if existing is not None:
            return _existing_inbound_result(
                session,
                existing,
                patient_id=patient_id,
                appointment_id=appointment_id,
                action_type=action,
                slot_id=slot.id,
                request_hash=request_hash,
            )

        status = BookingActionStatus.PENDING if require_staff_approval else BookingActionStatus.COMPLETED
        try:
            with session.begin_nested():
                appointment = _inbound_appointment(
                    session,
                    clinic_id,
                    patient_id=patient_id,
                    appointment_id=appointment_id,
                    slot=slot,
                    action_type=action,
                )
                booking = _new_booking_action(
                    clinic_id=clinic_id,
                    appointment_id=appointment.id,
                    outreach_job_id=None,
                    slot_id=slot.id,
                    action_type=action,
                    status=status,
                    request_hash=request_hash,
                    identity_decision=identity_decision,
                )
                session.add(booking)
                session.flush()
                if status == BookingActionStatus.PENDING:
                    ensure_handoff_receipt(session, clinic_id, booking, now=now)
                audit_action(
                    session,
                    clinic_id,
                    AuditAction.BOOK_APPOINTMENT,
                    booking.id,
                    {
                        "appointment_id": appointment.id,
                        "slot_id": slot.id,
                        "action_type": action.value,
                        "status": status.value,
                        "request_hash": request_hash,
                        "write_back_state": BookingWriteBackState.NOT_ATTEMPTED.value,
                        "written_back": False,
                        "channel": "sms",
                        "occurred_at": now,
                    },
                    actor="system:inbound-sms-booking",
                )
                session.flush()
        except IntegrityError:
            return _claim_conflict_result(
                session,
                slot.id,
                request_hash=request_hash,
            )
        queue_after_commit(
            session,
            "voice.booking.created",
            {
                "channel": "sms",
                "action_type": action.value,
                "status": status.value,
                "queued_for_staff": require_staff_approval,
            },
        )
        return _successful_result(booking)


def _inbound_appointment(
    session: Session,
    clinic_id: str,
    *,
    patient_id: str,
    appointment_id: str | None,
    slot: AvailabilitySlot,
    action_type: BookingActionType,
) -> Appointment:
    if action_type == BookingActionType.RESCHEDULE:
        if not appointment_id:
            raise LookupError("appointment is required for inbound reschedule")
        appointment = _load_patient_appointment(session, appointment_id, patient_id)
        if appointment is None:
            raise LookupError("appointment does not belong to this patient and clinic")
        return appointment

    source_ref = f"inbound-sms:{slot.id}"
    existing = session.execute(
        tenant_select(Appointment).where(Appointment.source_ref == source_ref)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.patient_id != patient_id:
            raise LookupError("inbound appointment source already belongs to another patient")
        return existing

    appointment = Appointment(
        id=f"appointment-{uuid.uuid4().hex}",
        clinic_id=clinic_id,
        patient_id=patient_id,
        source_ref=source_ref,
        status=AppointmentStatus.SCHEDULED,
        start_at=slot.start_at,
    )
    session.add(appointment)
    session.flush()
    return appointment


def _create_booking_action(
    session: Session,
    clinic_id: str,
    *,
    patient_id: str,
    outreach_job_id: str,
    slot_id: str,
    now: datetime,
    action_type: BookingActionType,
    appointment_id: str | None,
    require_staff_approval: bool,
    write_back_enabled: bool,
    identity_service: IdentityEvidenceService | None,
    identity_context: IdentityAuthorizationContext | None,
) -> BookingResult:
    _require_aware("now", now)
    with clinic_scope(session, clinic_id):
        patient = _load_patient(session, patient_id)
        if patient is None:
            raise LookupError(f"patient {patient_id!r} not found for clinic")
        try:
            assert_patient_writable(session, clinic_id, patient_id)
        except SubjectFrozenError:
            return BookingResult(False, error="subject_frozen")
        if patient.opt_out_flags.get("call") is True:
            return BookingResult(False, error="patient_opted_out")
        if patient.consent_flags.get("call") is not True:
            return BookingResult(False, error="no_call_consent")
        identity_decision = _authorize_booking(
            session,
            clinic_id=clinic_id,
            patient_id=patient_id,
            action=(
                IdentityAction.BOOK_SLOT
                if action_type == BookingActionType.BOOK
                else IdentityAction.RESCHEDULE
            ),
            identity_service=identity_service,
            identity_context=identity_context,
        )
        if not identity_decision.allowed:
            return BookingResult(False, error="identity_t2_required")

        job = _load_job(session, outreach_job_id, patient_id)
        if job is None:
            raise LookupError(f"outreach job {outreach_job_id!r} not found for patient")
        target_appointment_id = appointment_id or job.appointment_id
        if target_appointment_id is None:
            return BookingResult(False, error="missing_appointment")
        appointment = _load_patient_appointment(session, target_appointment_id, patient_id)
        if appointment is None:
            raise LookupError("appointment does not belong to this patient and clinic")

        slot = _load_slot(session, slot_id, for_update=True)
        if slot is None:
            raise LookupError(f"availability slot {slot_id!r} not found for clinic")
        if not _slot_is_fresh(slot, now):
            return BookingResult(False, error="slot_stale")
        slot_start = _as_utc(slot.start_at)
        slot_end = _as_utc(slot.end_at)
        if slot_start <= now or slot_end <= slot_start:
            return BookingResult(False, error="slot_unavailable")

        request_hash = canonical_booking_request_hash(
            clinic_id=clinic_id,
            patient_id=patient_id,
            appointment_id=appointment.id,
            slot=slot,
            action_type=action_type,
            outreach_job_id=job.id,
        )
        existing = _existing_action_for_slot(session, slot.id)
        if existing is not None:
            if existing.request_hash == request_hash or (
                existing.request_hash is None and existing.outreach_job_id == job.id
            ):
                return _successful_result(
                    existing,
                    idempotent=True,
                    message="booking action already exists for this job",
                    provider_confirmed=is_provider_booking_verified(
                        session,
                        clinic_id=clinic_id,
                        action=existing,
                        identity_service=identity_service,
                    ),
                )
            return BookingResult(False, error="slot_already_booked")

        staff_owned = (
            require_staff_approval
            or not write_back_enabled
            or action_type == BookingActionType.RESCHEDULE
        )
        status = (
            BookingActionStatus.PENDING
            if staff_owned
            else BookingActionStatus.COMPLETED
        )
        try:
            with session.begin_nested():
                action = _new_booking_action(
                    clinic_id=clinic_id,
                    appointment_id=appointment.id,
                    outreach_job_id=job.id,
                    slot_id=slot.id,
                    action_type=action_type,
                    status=status,
                    request_hash=request_hash,
                    identity_decision=identity_decision,
                )
                session.add(action)
                session.flush()
                if staff_owned:
                    ensure_handoff_receipt(session, clinic_id, action, now=now)
                if write_back_enabled and not staff_owned:
                    enqueue_cliniko_booking_effect(
                        session,
                        clinic_id=clinic_id,
                        booking_action_id=action.id,
                        intent="create",
                        available_at=now,
                    )
                job.state = (
                    OutreachState.ESCALATED
                    if staff_owned
                    else OutreachState.COMPLETED
                )
                audit_action(
                    session,
                    clinic_id,
                    AuditAction.BOOK_APPOINTMENT,
                    action.id,
                    {
                        "outreach_job_id": job.id,
                        "appointment_id": appointment.id,
                        "slot_id": slot.id,
                        "action_type": action_type.value,
                        "status": status.value,
                        "request_hash": request_hash,
                        "write_back_state": action.write_back_state.value,
                        "written_back": False,
                        "occurred_at": now,
                    },
                    actor="system:booking",
                )
                session.flush()
        except IntegrityError:
            return _claim_conflict_result(
                session,
                slot.id,
                request_hash=request_hash,
            )
        queue_after_commit(
            session,
            "voice.booking.created",
            {
                "channel": "call",
                "action_type": action_type.value,
                "status": status.value,
                "queued_for_staff": staff_owned,
            },
        )
        message = None
        if staff_owned:
            message = (
                "reschedule_not_confirmed_staff_follow_up"
                if action_type == BookingActionType.RESCHEDULE
                else "booking_not_confirmed_staff_follow_up"
            )
        return _successful_result(action, message=message)


def _load_patient(session: Session, patient_id: str) -> Patient | None:
    return session.execute(
        tenant_select(Patient).where(Patient.id == patient_id)
    ).scalar_one_or_none()


def _load_job(session: Session, outreach_job_id: str, patient_id: str) -> OutreachJob | None:
    return session.execute(
        tenant_select(OutreachJob).where(
            OutreachJob.id == outreach_job_id,
            OutreachJob.patient_id == patient_id,
        )
    ).scalar_one_or_none()


def _load_patient_appointment(
    session: Session, appointment_id: str, patient_id: str
) -> Appointment | None:
    return session.execute(
        tenant_select(Appointment).where(
            Appointment.id == appointment_id,
            Appointment.patient_id == patient_id,
        )
    ).scalar_one_or_none()


def _load_slot(
    session: Session,
    slot_id: str,
    *,
    for_update: bool = False,
) -> AvailabilitySlot | None:
    statement = tenant_select(AvailabilitySlot).where(AvailabilitySlot.id == slot_id)
    if (
        for_update
        and session.bind is not None
        and session.bind.dialect.name == "postgresql"
    ):
        statement = statement.with_for_update()
    return session.execute(statement).scalar_one_or_none()


def _existing_action_for_slot(session: Session, slot_id: str) -> BookingAction | None:
    return session.execute(
        tenant_select(BookingAction)
        .where(
            BookingAction.availability_slot_id == slot_id,
            BookingAction.status != BookingActionStatus.REJECTED,
        )
        .order_by(BookingAction.created_at, BookingAction.id)
        .limit(1)
    ).scalar_one_or_none()


def _new_booking_action(
    *,
    clinic_id: str,
    appointment_id: str,
    outreach_job_id: str | None,
    slot_id: str,
    action_type: BookingActionType,
    status: BookingActionStatus,
    request_hash: str,
    identity_decision: IdentityDecision,
) -> BookingAction:
    if (
        not identity_decision.allowed
        or identity_decision.evidence_id is None
        or identity_decision.policy_version is None
        or identity_decision.revision is None
    ):
        raise ValueError("identity_t2_required")
    return BookingAction(
        id=f"booking-action-{uuid.uuid4().hex}",
        clinic_id=clinic_id,
        appointment_id=appointment_id,
        outreach_job_id=outreach_job_id,
        availability_slot_id=slot_id,
        type=action_type,
        status=status,
        written_back=False,
        write_back_state=BookingWriteBackState.NOT_ATTEMPTED,
        external_appointment_ref=None,
        request_hash=request_hash,
        identity_evidence_id=identity_decision.evidence_id,
        identity_policy_version=identity_decision.policy_version,
        identity_evidence_revision=identity_decision.revision,
        provider_attempted_at=None,
        read_back_verified_at=None,
        conflict_reason=None,
    )


def _authorize_booking(
    session: Session,
    *,
    clinic_id: str,
    patient_id: str,
    action: IdentityAction,
    identity_service: IdentityEvidenceService | None,
    identity_context: IdentityAuthorizationContext | None,
) -> IdentityDecision:
    if identity_service is None or identity_context is None:
        return IdentityDecision(
            allowed=False,
            tier=IdentityTier.T0,
            reason=IdentityEvidenceReason.MISSING_POLICY,
            evidence_id=None,
        )
    return identity_service.authorize(
        session,
        clinic_id=clinic_id,
        evidence_id=identity_context.evidence_id,
        session_id=identity_context.session_id,
        route_id=identity_context.route_id,
        channel=identity_context.channel,
        patient_id=patient_id,
        action=action,
    )


def _successful_result(
    action: BookingAction,
    *,
    idempotent: bool = False,
    message: str | None = None,
    provider_confirmed: bool = False,
) -> BookingResult:
    return BookingResult(
        True,
        booking_action_id=action.id,
        status=action.status,
        idempotent=idempotent,
        queued_for_staff=action.status == BookingActionStatus.PENDING,
        staff_handoff_created=action.status == BookingActionStatus.PENDING,
        local_action_recorded=True,
        write_back_state=action.write_back_state,
        provider_confirmed=provider_confirmed,
        message=message,
    )


def _existing_inbound_result(
    session: Session,
    action: BookingAction,
    *,
    patient_id: str,
    appointment_id: str | None,
    action_type: BookingActionType,
    slot_id: str,
    request_hash: str,
) -> BookingResult:
    idempotent = action.request_hash == request_hash
    if action.request_hash is None and action.outreach_job_id is None:
        appointment = _load_patient_appointment(
            session,
            action.appointment_id,
            patient_id,
        )
        if appointment is not None and action.type == action_type:
            idempotent = (
                action_type == BookingActionType.BOOK
                and appointment.source_ref == f"inbound-sms:{slot_id}"
            ) or (
                action_type == BookingActionType.RESCHEDULE
                and action.appointment_id == appointment_id
            )
    if idempotent:
        return _successful_result(
            action,
            idempotent=True,
            message="inbound booking action already exists for this appointment",
        )
    return BookingResult(False, error="slot_already_booked")


def _claim_conflict_result(
    session: Session,
    slot_id: str,
    *,
    request_hash: str,
) -> BookingResult:
    existing = _existing_action_for_slot(session, slot_id)
    if existing is None:
        return BookingResult(False, error="slot_claim_conflict")
    if existing.request_hash == request_hash:
        return _successful_result(existing, idempotent=True)
    return BookingResult(False, error="slot_already_booked")


def _slot_is_fresh(slot: AvailabilitySlot, now: datetime) -> bool:
    if (
        slot.source_provider is None
        or slot.business_id is None
        or slot.clinician_id is None
        or slot.appointment_type_id is None
        or slot.fetched_at is None
        or slot.expires_at is None
    ):
        return False
    observed_at = _as_utc(slot.fetched_at)
    expires_at = _as_utc(slot.expires_at)
    current = _as_utc(now)
    return observed_at <= current < expires_at


def _require_aware(field: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _rfc3339(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")