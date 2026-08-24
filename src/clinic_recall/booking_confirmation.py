"""Shared deterministic authority for provider booking verification."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from .booking_identity import canonical_booking_request_hash
from .db import clinic_scope, tenant_select
from .durable.cliniko_booking_state import preflight_zero_match_hash
from .durable.enqueue import (
    booking_confirmation_effect_identity,
    cliniko_booking_effect_identity,
)
from .enums import (
    BookingActionStatus,
    BookingWriteBackState,
    ExternalEffectState,
    ExternalEffectType,
)
from .identity_evidence import IdentityAction, IdentityEvidenceService
from .models import AvailabilitySlot, BookingAction, ExternalEffect, OutreachJob, Patient
from .sync.cliniko_booking import ExpectedAppointmentSignature, ObservedAppointment


def is_provider_booking_verified(
    session: Session,
    *,
    clinic_id: str,
    action: BookingAction,
    identity_service: IdentityEvidenceService | None = None,
) -> bool:
    """Require exact action and effect evidence before any confirmation claim."""
    if (
        action.clinic_id != clinic_id
        or action.status != BookingActionStatus.COMPLETED
        or action.write_back_state != BookingWriteBackState.VERIFIED
        or action.written_back is not True
        or not action.external_appointment_ref
        or action.provider_attempted_at is None
        or action.read_back_verified_at is None
        or not action.request_hash
    ):
        return False
    with clinic_scope(session, clinic_id):
        effects = list(
            session.execute(
                tenant_select(ExternalEffect).where(
                    ExternalEffect.aggregate_type == "booking_action",
                    ExternalEffect.aggregate_id == action.id,
                    ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING,
                )
            ).scalars()
        )
        job = (
            session.execute(
                tenant_select(OutreachJob).where(
                    OutreachJob.id == action.outreach_job_id
                )
            ).scalar_one_or_none()
            if action.outreach_job_id
            else None
        )
        slot = (
            session.execute(
                tenant_select(AvailabilitySlot).where(
                    AvailabilitySlot.id == action.availability_slot_id
                )
            ).scalar_one_or_none()
            if action.availability_slot_id
            else None
        )
        patient = (
            session.execute(
                tenant_select(Patient).where(Patient.id == job.patient_id)
            ).scalar_one_or_none()
            if job is not None
            else None
        )
    if (
        len(effects) != 1
        or job is None
        or job.appointment_id != action.appointment_id
        or slot is None
        or patient is None
    ):
        return False
    if identity_service is None or not identity_service.authorize_bound_action(
        session,
        clinic_id=clinic_id,
        evidence_id=action.identity_evidence_id,
        evidence_policy_version=action.identity_policy_version,
        evidence_revision=action.identity_evidence_revision,
        patient_id=patient.id,
        channel=job.channel,
        action=IdentityAction.PROVIDER_CONFIRMATION,
    ).allowed:
        return False
    effect = effects[0]
    try:
        expected_payload, expected_key, expected_request_hash = (
            cliniko_booking_effect_identity(
                booking_action_id=action.id,
                booking_request_hash=action.request_hash,
            )
        )
        expected_signature = ExpectedAppointmentSignature(
            patient_id=patient.source_ref,
            business_id=slot.business_id or "",
            practitioner_id=slot.clinician_id or "",
            appointment_type_id=slot.appointment_type_id or "",
            starts_at=_database_utc(slot.start_at),
            ends_at=_database_utc(slot.end_at),
        )
        if action.request_hash != canonical_booking_request_hash(
            clinic_id=clinic_id,
            patient_id=patient.id,
            appointment_id=action.appointment_id,
            slot=slot,
            action_type=action.type,
            outreach_job_id=job.id,
        ):
            return False
        expected_evidence_hash = ObservedAppointment(
            provider_id=action.external_appointment_ref,
            signature=expected_signature,
            active=True,
            updated_at=_database_utc(action.read_back_verified_at),
        ).completion_hash(expected_request_hash)
    except (TypeError, ValueError):
        return False
    return bool(
        effect.aggregate_type == "booking_action"
        and effect.aggregate_id == action.id
        and effect.payload_version == 1
        and effect.payload == expected_payload
        and effect.idempotency_key == expected_key
        and effect.request_hash == expected_request_hash
        and effect.preflight_evidence_hash
        == preflight_zero_match_hash(expected_request_hash)
        and effect.state == ExternalEffectState.SUCCEEDED
        and effect.provider_status == "verified"
        and effect.provider_resource_id == action.external_appointment_ref
        and effect.dispatch_started_at is not None
        and _database_utc(effect.dispatch_started_at)
        == _database_utc(action.provider_attempted_at)
        and effect.completed_at is not None
        and effect.completion_evidence_hash == expected_evidence_hash
    )


def is_booking_confirmation_effect_authorized(
    session: Session,
    *,
    clinic_id: str,
    effect: ExternalEffect,
    booking_action_id: str,
    identity_service: IdentityEvidenceService | None = None,
) -> bool:
    """Bind one SMS effect to the exact verified BookingAction evidence."""
    if (
        effect.clinic_id != clinic_id
        or effect.effect_type != ExternalEffectType.SMS
        or effect.aggregate_type != "outreach_job"
        or effect.payload_version != 1
    ):
        return False
    with clinic_scope(session, clinic_id):
        action = session.execute(
            tenant_select(BookingAction).where(
                BookingAction.id == booking_action_id,
                BookingAction.outreach_job_id == effect.aggregate_id,
            )
        ).scalar_one_or_none()
        booking_effects = (
            list(
                session.execute(
                    tenant_select(ExternalEffect).where(
                        ExternalEffect.aggregate_type == "booking_action",
                        ExternalEffect.aggregate_id == booking_action_id,
                        ExternalEffect.effect_type
                        == ExternalEffectType.CLINIKO_BOOKING,
                    )
                ).scalars()
            )
            if action is not None
            else []
        )
    if (
        action is None
        or len(booking_effects) != 1
        or not is_provider_booking_verified(
            session,
            clinic_id=clinic_id,
            action=action,
            identity_service=identity_service,
        )
    ):
        return False
    completion_evidence_hash = booking_effects[0].completion_evidence_hash
    if not completion_evidence_hash:
        return False
    try:
        payload, idempotency_key, request_hash = booking_confirmation_effect_identity(
            outreach_job_id=effect.aggregate_id,
            booking_action_id=booking_action_id,
            completion_evidence_hash=completion_evidence_hash,
        )
    except ValueError:
        return False
    return bool(
        effect.payload == payload
        and effect.idempotency_key == idempotency_key
        and effect.request_hash == request_hash
    )


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)