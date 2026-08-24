"""Shared deterministic state and exact verification for Cliniko booking effects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..booking_identity import canonical_booking_request_hash
from ..db import clinic_scope, tenant_select
from ..enums import (
    BookingActionStatus,
    BookingActionType,
    BookingWriteBackState,
    ExternalEffectState,
    ExternalEffectType,
)
from ..identity_evidence import IdentityAction, IdentityEvidenceService
from ..models import (
    AvailabilitySlot,
    BookingAction,
    ExternalEffect,
    OutreachJob,
    Patient,
)
from ..pilot_controls import JobPilotGate, pilot_gate_decision
from ..rights import assert_patient_writable
from ..sync.cliniko_booking import ExpectedAppointmentSignature, ObservedAppointment
from .enqueue import (
    cliniko_booking_effect_identity,
    enqueue_booking_confirmation_effect,
)


@dataclass(frozen=True)
class BookingDispatchContext:
    """Immutable trusted facts materialized before provider I/O."""

    effect_id: str
    action_id: str
    action_request_hash: str
    outreach_job_id: str
    expected: ExpectedAppointmentSignature = field(repr=False)


def preflight_zero_match_hash(request_hash: str) -> str:
    """Bind one zero-exact-match observation to the immutable effect request."""
    encoded = json.dumps(
        {
            "exact_active_match_count": 0,
            "request_hash": request_hash,
            "schema_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def load_dispatch_context(
    session: Session,
    *,
    clinic_id: str,
    effect_id: str,
    now: datetime,
    programme_gate: JobPilotGate,
    identity_service: IdentityEvidenceService | None = None,
) -> BookingDispatchContext:
    """Reload and validate all local create authority without provider I/O."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    with clinic_scope(session, clinic_id):
        effect = session.execute(
            tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
        ).scalar_one_or_none()
        if effect is None or effect.effect_type != ExternalEffectType.CLINIKO_BOOKING:
            raise ValueError("cliniko_booking_effect_invalid")
        action = session.execute(
            tenant_select(BookingAction).where(BookingAction.id == effect.aggregate_id)
        ).scalar_one_or_none()
        if (
            action is None
            or effect.aggregate_type != "booking_action"
            or effect.payload_version != 1
            or action.type != BookingActionType.BOOK
            or action.status != BookingActionStatus.COMPLETED
            or not action.request_hash
            or not action.outreach_job_id
            or not action.availability_slot_id
        ):
            raise ValueError("cliniko_booking_contract_invalid")
        expected_payload, expected_key, expected_hash = cliniko_booking_effect_identity(
            booking_action_id=action.id,
            booking_request_hash=action.request_hash,
        )
        if (
            effect.payload != expected_payload
            or effect.idempotency_key != expected_key
            or effect.request_hash != expected_hash
        ):
            raise ValueError("cliniko_booking_request_identity_invalid")
        job = session.execute(
            tenant_select(OutreachJob).where(OutreachJob.id == action.outreach_job_id)
        ).scalar_one_or_none()
        slot = session.execute(
            tenant_select(AvailabilitySlot).where(
                AvailabilitySlot.id == action.availability_slot_id
            )
        ).scalar_one_or_none()
        if job is None or slot is None:
            raise ValueError("cliniko_booking_context_missing")
        patient = session.execute(
            tenant_select(Patient).where(Patient.id == job.patient_id)
        ).scalar_one_or_none()
        if patient is None:
            raise ValueError("cliniko_booking_context_missing")
        if job.appointment_id != action.appointment_id:
            raise ValueError("booking_request_identity_changed")
        current_request_hash = canonical_booking_request_hash(
            clinic_id=clinic_id,
            patient_id=patient.id,
            appointment_id=action.appointment_id,
            slot=slot,
            action_type=action.type,
            outreach_job_id=job.id,
        )
        if current_request_hash != action.request_hash:
            raise ValueError("booking_request_identity_changed")
        assert_patient_writable(session, clinic_id, patient.id)
        if patient.opt_out_flags.get("call") is True or patient.consent_flags.get("call") is not True:
            raise ValueError("booking_contact_gate_blocked")
        decision = pilot_gate_decision(programme_gate(session, clinic_id, job, now))
        if not decision.allowed:
            raise ValueError(decision.reason)
        if identity_service is None or not identity_service.authorize_bound_action(
            session,
            clinic_id=clinic_id,
            evidence_id=action.identity_evidence_id,
            evidence_policy_version=action.identity_policy_version,
            evidence_revision=action.identity_evidence_revision,
            patient_id=patient.id,
            channel=job.channel,
            action=IdentityAction.PROVIDER_EFFECT,
        ).allowed:
            raise ValueError("identity_t2_required")
        if (
            slot.source_provider != "cliniko"
            or not slot.business_id
            or not slot.clinician_id
            or not slot.appointment_type_id
            or slot.fetched_at is None
            or slot.expires_at is None
            or _database_utc(slot.fetched_at) > now
            or _database_utc(slot.expires_at) <= now
        ):
            raise ValueError("slot_not_authoritative")
        return BookingDispatchContext(
            effect_id=effect.id,
            action_id=action.id,
            action_request_hash=action.request_hash,
            outreach_job_id=job.id,
            expected=ExpectedAppointmentSignature(
                patient_id=patient.source_ref,
                business_id=slot.business_id,
                practitioner_id=slot.clinician_id,
                appointment_type_id=slot.appointment_type_id,
                starts_at=_database_utc(slot.start_at),
                ends_at=_database_utc(slot.end_at),
            ),
        )


def _database_utc(value: datetime) -> datetime:
    """Normalize trusted database timestamps across SQLite/PostgreSQL drivers."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def finalize_verified(
    session: Session,
    *,
    clinic_id: str,
    context: BookingDispatchContext,
    observed: ObservedAppointment,
    now: datetime,
    programme_gate: JobPilotGate,
    confirmation_release_enabled: bool,
    identity_service: IdentityEvidenceService | None = None,
) -> bool:
    """Atomically settle exact evidence and optionally release one SMS effect."""
    exact_match = observed.matches(context.expected)
    with clinic_scope(session, clinic_id):
        effect_query = tenant_select(ExternalEffect).where(
            ExternalEffect.id == context.effect_id
        )
        action_query = tenant_select(BookingAction).where(
            BookingAction.id == context.action_id
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            effect_query = effect_query.with_for_update()
            action_query = action_query.with_for_update()
        effect = session.execute(effect_query).scalar_one()
        action = session.execute(action_query).scalar_one()
        job = session.execute(
            tenant_select(OutreachJob).where(
                OutreachJob.id == context.outreach_job_id
            )
        ).scalar_one_or_none()
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
            action.status != BookingActionStatus.COMPLETED
            or action.outreach_job_id != context.outreach_job_id
            or job is None
            or job.appointment_id != action.appointment_id
            or slot is None
            or patient is None
        ):
            raise ValueError("verification_context_changed")
        assert_patient_writable(session, clinic_id, patient.id)
        if (
            patient.opt_out_flags.get("call") is True
            or patient.consent_flags.get("call") is not True
        ):
            raise ValueError("verification_contact_gate_blocked")
        decision = pilot_gate_decision(programme_gate(session, clinic_id, job, now))
        if not decision.allowed:
            raise ValueError(decision.reason)
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
            raise ValueError("identity_t2_required")
        current_expected = ExpectedAppointmentSignature(
            patient_id=patient.source_ref,
            business_id=slot.business_id or "",
            practitioner_id=slot.clinician_id or "",
            appointment_type_id=slot.appointment_type_id or "",
            starts_at=_database_utc(slot.start_at),
            ends_at=_database_utc(slot.end_at),
        )
        if current_expected != context.expected:
            raise ValueError("verification_context_changed")
        current_request_hash = canonical_booking_request_hash(
            clinic_id=clinic_id,
            patient_id=patient.id,
            appointment_id=action.appointment_id,
            slot=slot,
            action_type=action.type,
            outreach_job_id=job.id,
        )
        if current_request_hash != action.request_hash:
            raise ValueError("verification_context_changed")
        expected_payload, expected_key, expected_hash = cliniko_booking_effect_identity(
            booking_action_id=action.id,
            booking_request_hash=action.request_hash or "",
        )
        if (
            effect.aggregate_type != "booking_action"
            or effect.aggregate_id != action.id
            or effect.effect_type != ExternalEffectType.CLINIKO_BOOKING
            or effect.payload_version != 1
            or effect.payload != expected_payload
            or effect.idempotency_key != expected_key
            or effect.request_hash != expected_hash
            or effect.preflight_evidence_hash
            != preflight_zero_match_hash(effect.request_hash)
        ):
            _quarantine_booking_conflict(
                session,
                effect=effect,
                action=action,
                now=now,
                reason_code="booking_request_identity_conflict",
            )
            return False
        evidence_hash = observed.completion_hash(effect.request_hash)
        if effect.state == ExternalEffectState.SUCCEEDED:
            if (
                not exact_match
                or effect.provider_resource_id != observed.provider_id
                or effect.completion_evidence_hash != evidence_hash
                or action.external_appointment_ref != observed.provider_id
            ):
                _quarantine_booking_conflict(
                    session,
                    effect=effect,
                    action=action,
                    now=now,
                    reason_code="verified_evidence_conflict",
                )
            return False
        if not exact_match:
            raise ValueError("read_back_mismatch")
        if effect.state not in {
            ExternalEffectState.DISPATCHING,
            ExternalEffectState.RECONCILE_REQUIRED,
        }:
            raise ValueError("effect_not_verifiable")
        if action.request_hash != context.action_request_hash:
            raise ValueError("booking_request_changed")
        action.write_back_state = BookingWriteBackState.VERIFIED
        action.written_back = True
        action.external_appointment_ref = observed.provider_id
        action.read_back_verified_at = now
        action.conflict_reason = None
        effect.state = ExternalEffectState.SUCCEEDED
        effect.provider_resource_id = observed.provider_id
        effect.provider_status = "verified"
        effect.completion_evidence_hash = evidence_hash
        effect.completed_at = now
        effect.last_error_class = None
        effect.last_error_code = None
        effect.lease_owner = None
        effect.lease_expires_at = None
        if confirmation_release_enabled:
            if (
                patient.consent_flags.get("sms") is True
                and patient.opt_out_flags.get("sms") is not True
            ):
                enqueue_booking_confirmation_effect(
                    session,
                    clinic_id=clinic_id,
                    outreach_job_id=job.id,
                    booking_action_id=action.id,
                    completion_evidence_hash=evidence_hash,
                    available_at=now,
                )
        session.flush()
        return True


def _quarantine_booking_conflict(
    session: Session,
    *,
    effect: ExternalEffect,
    action: BookingAction,
    now: datetime,
    reason_code: str,
) -> None:
    effect.state = ExternalEffectState.RECONCILE_REQUIRED
    effect.provider_status = reason_code
    effect.last_error_class = "VerificationConflict"
    effect.last_error_code = reason_code
    effect.lease_owner = None
    effect.lease_expires_at = None
    action.write_back_state = BookingWriteBackState.CONFLICT
    action.written_back = False
    action.read_back_verified_at = None
    action.conflict_reason = reason_code
    if action.outreach_job_id:
        confirmation_effects = list(
            session.execute(
                tenant_select(ExternalEffect).where(
                    ExternalEffect.effect_type == ExternalEffectType.SMS,
                    ExternalEffect.aggregate_type == "outreach_job",
                    ExternalEffect.aggregate_id == action.outreach_job_id,
                )
            ).scalars()
        )
        for confirmation in confirmation_effects:
            if confirmation.payload.get("booking_action_id") != action.id:
                continue
            if confirmation.state in {
                ExternalEffectState.PENDING,
                ExternalEffectState.LEASED,
            }:
                confirmation.state = ExternalEffectState.CANCELED
                confirmation.provider_status = "not_dispatched"
                confirmation.last_error_class = "VerificationConflict"
                confirmation.last_error_code = reason_code
                confirmation.completed_at = now
                confirmation.lease_owner = None
                confirmation.lease_expires_at = None
            elif confirmation.state == ExternalEffectState.DISPATCHING:
                confirmation.state = ExternalEffectState.RECONCILE_REQUIRED
                confirmation.last_error_class = "VerificationConflict"
                confirmation.last_error_code = reason_code
                confirmation.lease_owner = None
                confirmation.lease_expires_at = None
    from ..handoffs import ensure_external_effect_handoff

    ensure_external_effect_handoff(
        session,
        effect,
        reason_code=reason_code,
        now=now,
    )
    session.flush()