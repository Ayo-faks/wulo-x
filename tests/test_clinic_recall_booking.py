"""Tests for Phase 3 deterministic availability, booking, and escalation."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
from sqlalchemy import func, select
from src.clinic_recall import booking as booking_module
from src.clinic_recall.availability import (
    AvailabilitySlotInput,
    get_availability,
    upsert_availability_slots,
)
from src.clinic_recall.booking import (
    book_inbound_slot as _book_inbound_slot,
)
from src.clinic_recall.booking import (
    book_slot as _book_slot,
)
from src.clinic_recall.booking import (
    reschedule as _reschedule,
)
from src.clinic_recall.enums import (
    AuditAction,
    BookingActionStatus,
    BookingWriteBackState,
    CampaignStatus,
    CampaignType,
    Channel,
    EscalationPriority,
    EscalationReason,
    ExternalEffectType,
    InteractionOutcome,
    OutreachState,
)
from src.clinic_recall.escalation import escalate_to_staff
from src.clinic_recall.messaging.send import (
    send_sms_confirmation as _send_sms_confirmation,
)
from src.clinic_recall.messaging.sender import FakeMessageSender
from src.clinic_recall.models import (
    Appointment,
    AuditLog,
    BookingAction,
    Campaign,
    Clinic,
    Escalation,
    ExternalEffect,
    HandoffReceipt,
    Interaction,
    OutreachJob,
    Patient,
)
from src.clinic_recall.pilot_controls import PilotGateDecision
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectKey,
    SubjectKeyring,
    request_patient_erasure,
)

from tests.identity_evidence_support import grant_synthetic_t2

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
_IDENTITY_SUFFIX = count(1)


def _allow_pilot(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def send_sms_confirmation(*args, **kwargs):
    kwargs.setdefault("pilot_gate", _allow_pilot)
    return _send_sms_confirmation(*args, **kwargs)


def _with_synthetic_t2(session, clinic_id: str, kwargs: dict, channel: Channel):
    if kwargs.get("identity_service") is not None:
        return kwargs
    patient_id = str(kwargs["patient_id"])
    service, context = grant_synthetic_t2(
        session,
        clinic_id=clinic_id,
        patient_id=patient_id,
        channel=channel,
        now=kwargs["now"],
        suffix=f"booking-suite-{next(_IDENTITY_SUFFIX)}",
    )
    return {
        **kwargs,
        "identity_service": service,
        "identity_context": context,
    }


def book_slot(session, clinic_id: str, **kwargs):
    return _book_slot(
        session,
        clinic_id,
        **_with_synthetic_t2(session, clinic_id, kwargs, Channel.CALL),
    )


def reschedule(session, clinic_id: str, **kwargs):
    return _reschedule(
        session,
        clinic_id,
        **_with_synthetic_t2(session, clinic_id, kwargs, Channel.CALL),
    )


def book_inbound_slot(session, clinic_id: str, **kwargs):
    return _book_inbound_slot(
        session,
        clinic_id,
        **_with_synthetic_t2(session, clinic_id, kwargs, Channel.SMS),
    )


def _seed_voice_job(sqlite_session, *, clinic_id: str = "clinic-voice") -> str:
    suffix = "".join(ch for ch in clinic_id if ch.isdigit()) or str(abs(hash(clinic_id)) % 10000)
    sqlite_session.add(
        Clinic(
            id=clinic_id,
            name="Voice Clinic",
            sms_number=f"+44770091{int(suffix) % 10000:04d}",
            timezone="Europe/London",
            daily_caps=200,
        )
    )
    sqlite_session.add(
        Patient(
            id=f"patient-{clinic_id}",
            clinic_id=clinic_id,
            source_ref=f"P-{clinic_id}",
            name="Voice Patient",
            phone="+447700910001",
            email="voice@example.test",
            consent_flags={"call": True, "sms": True, "email": True},
            opt_out_flags={},
        )
    )
    sqlite_session.add(
        Appointment(
            id=f"appointment-{clinic_id}",
            clinic_id=clinic_id,
            patient_id=f"patient-{clinic_id}",
            source_ref=f"A-{clinic_id}",
            status="missed",
            start_at=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
        )
    )
    sqlite_session.add(
        Campaign(
            id=f"campaign-{clinic_id}",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        OutreachJob(
            id=f"job-{clinic_id}",
            clinic_id=clinic_id,
            campaign_id=f"campaign-{clinic_id}",
            patient_id=f"patient-{clinic_id}",
            appointment_id=f"appointment-{clinic_id}",
            channel=Channel.CALL,
            state=OutreachState.NO_REPLY,
        )
    )
    sqlite_session.flush()
    return clinic_id


def _add_slots(sqlite_session, clinic_id: str) -> list[str]:
    summaries = upsert_availability_slots(
        sqlite_session,
        clinic_id,
        [
            AvailabilitySlotInput(
                source_ref="cliniko-slot-1",
                source_provider="cliniko",
                business_id="920000001",
                appointment_type_id="940000001",
                clinician_id="930000001",
                start_at=NOW + timedelta(days=1),
                end_at=NOW + timedelta(days=1, minutes=30),
                fetched_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            ),
            AvailabilitySlotInput(
                source_ref="cliniko-slot-2",
                source_provider="cliniko",
                business_id="920000002",
                appointment_type_id="940000002",
                clinician_id="930000002",
                start_at=NOW + timedelta(days=2),
                end_at=NOW + timedelta(days=2, minutes=30),
                fetched_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            ),
        ],
        now=NOW,
    )
    return [summary.slot_id for summary in summaries]


def test_enabled_create_atomically_enqueues_one_minimized_cliniko_effect(
    sqlite_session,
) -> None:
    clinic_id = _seed_voice_job(sqlite_session)
    slot_id = _add_slots(sqlite_session, clinic_id)[0]

    first = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_id,
        now=NOW,
        write_back_enabled=True,
    )
    second = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_id,
        now=NOW,
        write_back_enabled=True,
    )

    action = sqlite_session.get(BookingAction, first.booking_action_id)
    effects = list(
        sqlite_session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalars()
    )
    assert first.success is True
    assert first.write_back_state == BookingWriteBackState.PENDING
    assert first.provider_confirmed is False
    assert second.idempotent is True
    assert action is not None and action.write_back_state == BookingWriteBackState.PENDING
    assert len(effects) == 1
    assert effects[0].aggregate_type == "booking_action"
    assert effects[0].aggregate_id == action.id
    assert effects[0].payload == {"intent": "create", "booking_action_id": action.id}
    assert "patient-clinic-voice" not in str(effects[0].payload)
    assert "cliniko-slot" not in str(effects[0].payload)


def test_default_off_create_is_staff_owned_and_has_no_provider_effect(
    sqlite_session,
) -> None:
    clinic_id = _seed_voice_job(sqlite_session)
    slot_id = _add_slots(sqlite_session, clinic_id)[0]

    result = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_id,
        now=NOW,
    )

    action = sqlite_session.get(BookingAction, result.booking_action_id)
    assert result.success is True
    assert result.queued_for_staff is True
    assert result.staff_handoff_created is True
    assert result.write_back_state == BookingWriteBackState.NOT_ATTEMPTED
    assert result.provider_confirmed is False
    assert result.message == "booking_not_confirmed_staff_follow_up"
    assert action is not None and action.status == BookingActionStatus.PENDING
    receipt = sqlite_session.scalar(select(HandoffReceipt))
    effects = list(sqlite_session.scalars(select(ExternalEffect)))
    assert receipt is not None and receipt.booking_action_id == action.id
    assert len(effects) == 1
    assert effects[0].effect_type == ExternalEffectType.HANDOFF_NOTIFICATION


def test_reschedule_remains_staff_only_without_atomic_provider_precondition(
    sqlite_session,
) -> None:
    clinic_id = _seed_voice_job(sqlite_session)
    slot_id = _add_slots(sqlite_session, clinic_id)[0]

    result = reschedule(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        appointment_id="appointment-clinic-voice",
        slot_id=slot_id,
        now=NOW,
        write_back_enabled=True,
    )

    action = sqlite_session.get(BookingAction, result.booking_action_id)
    assert result.success is True
    assert result.queued_for_staff is True
    assert result.staff_handoff_created is True
    assert result.provider_confirmed is False
    assert result.message == "reschedule_not_confirmed_staff_follow_up"
    assert action is not None and action.status == BookingActionStatus.PENDING
    assert action.write_back_state == BookingWriteBackState.NOT_ATTEMPTED
    receipt = sqlite_session.scalar(select(HandoffReceipt))
    effects = list(sqlite_session.scalars(select(ExternalEffect)))
    assert receipt is not None and receipt.booking_action_id == action.id
    assert len(effects) == 1
    assert effects[0].effect_type == ExternalEffectType.HANDOFF_NOTIFICATION


def test_booking_action_effect_and_audit_roll_back_together_on_enqueue_failure(
    sqlite_session,
    monkeypatch,
) -> None:
    clinic_id = _seed_voice_job(sqlite_session)
    slot_id = _add_slots(sqlite_session, clinic_id)[0]

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("synthetic enqueue failure")

    monkeypatch.setattr(
        booking_module,
        "enqueue_cliniko_booking_effect",
        fail_enqueue,
    )

    with pytest.raises(RuntimeError, match="synthetic enqueue failure"):
        book_slot(
            sqlite_session,
            clinic_id,
            patient_id="patient-clinic-voice",
            outreach_job_id="job-clinic-voice",
            slot_id=slot_id,
            now=NOW,
            write_back_enabled=True,
        )

    assert sqlite_session.scalar(select(func.count()).select_from(BookingAction)) == 0
    assert sqlite_session.scalar(select(func.count()).select_from(ExternalEffect)) == 0
    assert sqlite_session.scalar(select(func.count()).select_from(AuditLog)) == 0
    assert sqlite_session.get(OutreachJob, "job-clinic-voice").state == OutreachState.NO_REPLY


def test_get_availability_returns_only_real_unbooked_slots(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    slot_ids = _add_slots(sqlite_session, clinic_id)

    first_booking = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_ids[0],
        now=NOW,
    )
    slots = get_availability(
        sqlite_session,
        clinic_id,
        now=NOW,
        window_start=NOW,
        window_end=NOW + timedelta(days=7),
    )

    assert first_booking.success is True
    assert [slot.slot_id for slot in slots] == [slot_ids[1]]


def test_core_get_availability_rejects_naive_window(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    _add_slots(sqlite_session, clinic_id)

    with pytest.raises(ValueError):
        get_availability(
            sqlite_session,
            clinic_id,
            now=NOW,
            window_start=NOW.replace(tzinfo=None),
            window_end=NOW + timedelta(days=7),
        )


def test_expired_slot_is_not_offered_or_claimed(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    slot = upsert_availability_slots(
        sqlite_session,
        clinic_id,
        [
            AvailabilitySlotInput(
                source_ref="cliniko-expired-slot",
                source_provider="cliniko",
                business_id="920000001",
                appointment_type_id="940000001",
                clinician_id="930000001",
                start_at=NOW + timedelta(days=1),
                end_at=NOW + timedelta(days=1, minutes=30),
                fetched_at=NOW - timedelta(minutes=10),
                expires_at=NOW,
            )
        ],
        now=NOW,
    )[0]

    offered = get_availability(
        sqlite_session,
        clinic_id,
        now=NOW,
        window_start=NOW,
        window_end=NOW + timedelta(days=7),
    )
    claimed = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot.slot_id,
        now=NOW,
    )

    assert offered == []
    assert claimed.success is False
    assert claimed.error == "slot_stale"


def test_book_slot_is_idempotent_and_blocks_double_booking(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    slot_id = _add_slots(sqlite_session, clinic_id)[0]

    first = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_id,
        now=NOW,
    )
    second = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_id,
        now=NOW,
    )
    _seed_voice_job(sqlite_session, clinic_id="clinic-voice-2")

    blocked = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_id,
        now=NOW,
    )

    assert first.success is True
    assert second.success is True
    assert second.idempotent is True
    assert blocked.success is True
    assert sqlite_session.get(OutreachJob, "job-clinic-voice").state == OutreachState.ESCALATED
    assert sqlite_session.execute(select(func.count()).select_from(BookingAction)).scalar() == 1
    action = sqlite_session.execute(select(BookingAction)).scalar_one()
    assert action.write_back_state is BookingWriteBackState.NOT_ATTEMPTED
    assert action.written_back is False
    assert action.external_appointment_ref is None
    assert action.provider_attempted_at is None
    assert action.read_back_verified_at is None
    assert action.conflict_reason is None
    assert re.fullmatch(r"[0-9a-f]{64}", action.request_hash)
    assert first.local_action_recorded is True
    assert first.provider_confirmed is False
    assert first.write_back_state is BookingWriteBackState.NOT_ATTEMPTED
    assert second.write_back_state is BookingWriteBackState.NOT_ATTEMPTED
    assert sqlite_session.execute(select(AuditLog.action)).scalars().all() == [
        AuditAction.BOOK_APPOINTMENT
    ]


def test_frozen_patient_cannot_create_booking_actions(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    slot_id = _add_slots(sqlite_session, clinic_id)[0]
    patient_id = f"patient-{clinic_id}"
    request_patient_erasure(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id=patient_id,
        confirm_token=f"ERASE {patient_id}",
        request_identity="tests-booking-freeze",
        actor_role="dpo",
        actor_reference="tests-booking-operator",
        keyring=SubjectKeyring(
            current=SubjectKey(
                version="tests-booking-v1",
                secret=b"tests-booking-rights-key",
            )
        ),
        policy=RightsPolicy(
            version="tests-booking-policy-v1",
            approval_evidence_hash="a" * 64,
            request_due_after=timedelta(days=28),
        ),
        now=NOW,
    )

    outbound = book_slot(
        sqlite_session,
        clinic_id,
        patient_id=patient_id,
        outreach_job_id=f"job-{clinic_id}",
        slot_id=slot_id,
        now=NOW,
    )
    inbound = book_inbound_slot(
        sqlite_session,
        clinic_id,
        patient_id=patient_id,
        appointment_id=f"appointment-{clinic_id}",
        slot_id=slot_id,
        now=NOW,
        action_type="reschedule",
    )

    assert outbound.success is False
    assert outbound.error == "subject_frozen"
    assert inbound.success is False
    assert inbound.error == "subject_frozen"
    assert sqlite_session.execute(select(func.count()).select_from(BookingAction)).scalar() == 0


def test_book_inbound_slot_creates_new_appointment_with_sms_consent(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    slot_id = _add_slots(sqlite_session, clinic_id)[0]

    result = book_inbound_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        slot_id=slot_id,
        now=NOW,
        action_type="book",
    )

    action = sqlite_session.execute(select(BookingAction)).scalar_one()
    appointments = sqlite_session.execute(select(Appointment).order_by(Appointment.source_ref)).scalars().all()
    new_appointment = next(appointment for appointment in appointments if appointment.source_ref.startswith("inbound-sms:"))
    assert result.success is True
    assert result.status == BookingActionStatus.COMPLETED
    assert action.appointment_id == new_appointment.id
    assert action.outreach_job_id is None
    assert action.availability_slot_id == slot_id
    assert new_appointment.patient_id == "patient-clinic-voice"
    assert new_appointment.status == "scheduled"
    assert sqlite_session.execute(select(AuditLog.action)).scalars().all() == [
        AuditAction.BOOK_APPOINTMENT
    ]


def test_book_inbound_slot_enforces_sms_consent(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    patient = sqlite_session.get(Patient, "patient-clinic-voice")
    patient.consent_flags = {"call": True}
    slot_id = _add_slots(sqlite_session, clinic_id)[0]

    result = book_inbound_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        appointment_id="appointment-clinic-voice",
        slot_id=slot_id,
        now=NOW,
        action_type="reschedule",
    )

    assert result.success is False
    assert result.error == "no_sms_consent"
    assert sqlite_session.execute(select(func.count()).select_from(BookingAction)).scalar() == 0


def test_book_inbound_slot_enforces_sms_opt_out(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    patient = sqlite_session.get(Patient, "patient-clinic-voice")
    patient.opt_out_flags = {"sms": True}
    slot_id = _add_slots(sqlite_session, clinic_id)[0]

    result = book_inbound_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        appointment_id="appointment-clinic-voice",
        slot_id=slot_id,
        now=NOW,
        action_type="reschedule",
    )

    assert result.success is False
    assert result.error == "patient_sms_opted_out"
    assert sqlite_session.execute(select(func.count()).select_from(BookingAction)).scalar() == 0


def test_book_slot_blocks_another_job_from_double_booking(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    slot_id = _add_slots(sqlite_session, clinic_id)[0]
    sqlite_session.add(
        Patient(
            id="patient-other",
            clinic_id=clinic_id,
            source_ref="P-OTHER",
            name="Other Patient",
            phone="+447700910002",
            consent_flags={"call": True},
            opt_out_flags={},
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-other",
            clinic_id=clinic_id,
            patient_id="patient-other",
            source_ref="A-OTHER",
            status="missed",
            start_at=datetime(2026, 6, 21, 9, 0, tzinfo=UTC),
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-other",
            clinic_id=clinic_id,
            campaign_id="campaign-clinic-voice",
            patient_id="patient-other",
            appointment_id="appointment-other",
            channel=Channel.CALL,
            state=OutreachState.NO_REPLY,
        )
    )
    sqlite_session.flush()

    first = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_id,
        now=NOW,
    )
    blocked = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-other",
        outreach_job_id="job-other",
        slot_id=slot_id,
        now=NOW,
    )

    assert first.success is True
    assert blocked.success is False
    assert blocked.error == "slot_already_booked"
    assert sqlite_session.execute(select(func.count()).select_from(BookingAction)).scalar() == 1


def test_rolled_back_booking_claim_leaves_slot_claimable(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    slot_id = _add_slots(sqlite_session, clinic_id)[0]
    savepoint = sqlite_session.begin_nested()

    first = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_id,
        now=NOW,
    )
    savepoint.rollback()
    sqlite_session.expire_all()

    assert first.success is True
    assert sqlite_session.execute(select(func.count()).select_from(BookingAction)).scalar() == 0
    assert sqlite_session.execute(select(func.count()).select_from(AuditLog)).scalar() == 0
    assert sqlite_session.get(OutreachJob, "job-clinic-voice").state == OutreachState.NO_REPLY

    retried = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_id,
        now=NOW,
    )

    assert retried.success is True
    assert retried.idempotent is False
    assert sqlite_session.execute(select(func.count()).select_from(BookingAction)).scalar() == 1


def test_book_slot_enforces_call_consent(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    patient = sqlite_session.get(Patient, "patient-clinic-voice")
    patient.consent_flags = {"sms": True}
    slot_id = _add_slots(sqlite_session, clinic_id)[0]

    result = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_id,
        now=NOW,
    )

    assert result.success is False
    assert result.error == "no_call_consent"
    assert sqlite_session.execute(select(func.count()).select_from(BookingAction)).scalar() == 0


def test_book_inbound_slot_reschedules_existing_appointment_with_sms_consent(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    slot_id = _add_slots(sqlite_session, clinic_id)[0]

    result = book_inbound_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        appointment_id="appointment-clinic-voice",
        slot_id=slot_id,
        now=NOW,
        action_type="reschedule",
    )

    action = sqlite_session.execute(select(BookingAction)).scalar_one()
    assert result.success is True
    assert result.status == BookingActionStatus.COMPLETED
    assert action.appointment_id == "appointment-clinic-voice"
    assert action.outreach_job_id is None
    assert action.availability_slot_id == slot_id
    assert action.status == BookingActionStatus.COMPLETED
    assert sqlite_session.execute(select(AuditLog.action)).scalars().all() == [
        AuditAction.BOOK_APPOINTMENT
    ]


def test_book_slot_does_not_cross_clinic_scope(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)
    other_clinic_id = _seed_voice_job(sqlite_session, clinic_id="clinic-other")
    slot_id = _add_slots(sqlite_session, clinic_id)[0]

    with pytest.raises(LookupError):
        book_slot(
            sqlite_session,
            other_clinic_id,
            patient_id="patient-clinic-other",
            outreach_job_id="job-clinic-other",
            slot_id=slot_id,
            now=NOW,
        )

    assert sqlite_session.execute(select(func.count()).select_from(BookingAction)).scalar() == 0


def test_escalate_to_staff_marks_job_escalated_and_audits(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)

    result = escalate_to_staff(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        reason=EscalationReason.CLINICAL,
        now=NOW,
        context="Patient asked whether their pain means something serious.",
    )

    job = sqlite_session.get(OutreachJob, "job-clinic-voice")
    escalation = sqlite_session.execute(select(Escalation)).scalar_one()
    interaction = sqlite_session.execute(select(Interaction)).scalar_one()

    assert result.escalation_id == escalation.id
    assert result.priority == EscalationPriority.HIGH
    assert job.state == OutreachState.ESCALATED
    assert escalation.reason == EscalationReason.CLINICAL
    assert interaction.outcome == InteractionOutcome.ROUTED_TO_STAFF
    assert sqlite_session.execute(select(AuditLog.action)).scalars().all() == [AuditAction.ESCALATE]


def test_local_voice_choice_never_sends_confirmation_before_provider_verification(
    sqlite_session,
):
    clinic_id = _seed_voice_job(sqlite_session)
    slot_id = _add_slots(sqlite_session, clinic_id)[0]
    sender = FakeMessageSender()

    booking = book_slot(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        slot_id=slot_id,
        now=NOW,
    )
    first_confirmation = send_sms_confirmation(
        sqlite_session, clinic_id, "job-clinic-voice", NOW, sender
    )
    second_confirmation = send_sms_confirmation(
        sqlite_session, clinic_id, "job-clinic-voice", NOW, sender
    )

    assert booking.success is True
    assert booking.status == BookingActionStatus.PENDING
    assert booking.provider_confirmed is False
    assert first_confirmation.sent is False
    assert first_confirmation.error == "provider_booking_not_verified"
    assert second_confirmation.sent is False
    assert sender.sms_messages == []


def test_clinical_voice_turn_escalates_with_no_booking(sqlite_session):
    clinic_id = _seed_voice_job(sqlite_session)

    escalation = escalate_to_staff(
        sqlite_session,
        clinic_id,
        patient_id="patient-clinic-voice",
        outreach_job_id="job-clinic-voice",
        reason=EscalationReason.AMBIGUOUS,
        now=NOW,
        context="Patient gave an unclear answer and mentioned a worrying symptom.",
    )

    assert escalation.reason == EscalationReason.AMBIGUOUS
    assert sqlite_session.execute(select(func.count()).select_from(BookingAction)).scalar() == 0
    assert sqlite_session.get(OutreachJob, "job-clinic-voice").state == OutreachState.ESCALATED