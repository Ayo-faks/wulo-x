"""Tests for Phase 4 staff approval and escalation queue services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from src.clinic_recall.availability import AvailabilitySlotInput, upsert_availability_slots
from src.clinic_recall.booking import book_slot
from src.clinic_recall.enums import (
    AuditAction,
    BookingActionStatus,
    BookingWriteBackState,
    CampaignStatus,
    CampaignType,
    Channel,
    EscalationReason,
    ExternalEffectType,
    OutreachState,
)
from src.clinic_recall.escalation import escalate_to_staff
from src.clinic_recall.identity_evidence import IdentityEvidenceService
from src.clinic_recall.messaging.sender import FakeMessageSender
from src.clinic_recall.models import (
    Appointment,
    AuditLog,
    BookingAction,
    Campaign,
    Clinic,
    ExternalEffect,
    OutreachJob,
    Patient,
)
from src.clinic_recall.pilot_controls import PilotGateDecision
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectFrozenError,
    SubjectKey,
    SubjectKeyring,
    request_patient_erasure,
)
from src.clinic_recall.staff_queue import (
    QueueDecision,
    list_staff_queue,
)
from src.clinic_recall.staff_queue import (
    resolve_queue_item as _resolve_queue_item,
)

from tests.identity_evidence_support import (
    grant_synthetic_t2,
    synthetic_identity_policy,
)

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _allow_pilot(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def _identity_service() -> IdentityEvidenceService:
    return IdentityEvidenceService(
        policy=synthetic_identity_policy(),
        clock=lambda: NOW,
        identifier_factory=lambda: "unused-staff-identity",
        challenge_factory=lambda: "unused-staff-challenge",
    )


def resolve_queue_item(*args, **kwargs):
    kwargs.setdefault("identity_service", _identity_service())
    return _resolve_queue_item(*args, **kwargs)


def _seed_job(session, clinic_id: str = "clinic-queue") -> tuple[str, str]:
    session.add(
        Clinic(
            id=clinic_id,
            name="Queue Clinic",
            sms_number=f"+447701{abs(hash(clinic_id)) % 1000000:06d}",
            timezone="Europe/London",
            daily_caps=200,
        )
    )
    session.add(
        Patient(
            id=f"patient-{clinic_id}",
            clinic_id=clinic_id,
            source_ref=f"P-{clinic_id}",
            name="Queue Patient",
            phone="+447700910010",
            email="queue@example.test",
            consent_flags={"call": True, "sms": True, "email": True},
            opt_out_flags={},
        )
    )
    session.add(
        Appointment(
            id=f"appointment-{clinic_id}",
            clinic_id=clinic_id,
            patient_id=f"patient-{clinic_id}",
            source_ref=f"A-{clinic_id}",
            status="missed",
            start_at=NOW - timedelta(days=7),
            value=50,
        )
    )
    session.add(
        Campaign(
            id=f"campaign-{clinic_id}",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    job_id = f"job-{clinic_id}"
    session.add(
        OutreachJob(
            id=job_id,
            clinic_id=clinic_id,
            campaign_id=f"campaign-{clinic_id}",
            patient_id=f"patient-{clinic_id}",
            appointment_id=f"appointment-{clinic_id}",
            channel=Channel.CALL,
            state=OutreachState.NO_REPLY,
        )
    )
    session.flush()
    slots = upsert_availability_slots(
        session,
        clinic_id,
        [
            AvailabilitySlotInput(
                source_ref=f"slot-{clinic_id}",
                source_provider="cliniko",
                business_id="920000001",
                clinician_id="clinician-a",
                appointment_type_id="940000001",
                start_at=NOW + timedelta(days=1),
                end_at=NOW + timedelta(days=1, minutes=30),
                fetched_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
        ],
        now=NOW,
    )
    return clinic_id, slots[0].slot_id


def _pending_booking(session, clinic_id: str, slot_id: str) -> str:
    identity_service, identity_context = grant_synthetic_t2(
        session,
        clinic_id=clinic_id,
        patient_id=f"patient-{clinic_id}",
        channel=Channel.CALL,
        now=NOW,
        suffix=f"staff-{clinic_id}",
    )
    result = book_slot(
        session,
        clinic_id,
        patient_id=f"patient-{clinic_id}",
        outreach_job_id=f"job-{clinic_id}",
        slot_id=slot_id,
        now=NOW,
        require_staff_approval=True,
        identity_service=identity_service,
        identity_context=identity_context,
    )
    assert result.success is True
    assert result.booking_action_id is not None
    return result.booking_action_id


def test_list_staff_queue_prioritizes_urgent_escalations_before_pending_approvals(sqlite_session):
    clinic_id, slot_id = _seed_job(sqlite_session)
    booking_action_id = _pending_booking(sqlite_session, clinic_id, slot_id)
    escalation = escalate_to_staff(
        sqlite_session,
        clinic_id,
        patient_id=f"patient-{clinic_id}",
        outreach_job_id=f"job-{clinic_id}",
        reason=EscalationReason.URGENT,
        now=NOW,
        context="Patient mentioned an urgent concern.",
    )

    items = list_staff_queue(sqlite_session, clinic_id)

    assert [item.item_id for item in items] == [
        f"escalation:{escalation.escalation_id}",
        f"booking_action:{booking_action_id}",
    ]
    assert items[0].priority == "high"
    assert items[0].reason == "urgent"
    assert items[0].context_summary == "call inbound routed_to_staff urgent"
    assert "urgent concern" not in items[0].model_dump_json()


def test_approve_pending_booking_enqueues_once_without_confirmation_and_audits(
    sqlite_session,
):
    clinic_id, slot_id = _seed_job(sqlite_session)
    booking_action_id = _pending_booking(sqlite_session, clinic_id, slot_id)
    sender = FakeMessageSender()

    first = resolve_queue_item(
        sqlite_session,
        clinic_id,
        f"booking_action:{booking_action_id}",
        QueueDecision.APPROVE,
        staff_actor="staff:alice",
        now=NOW,
        sender=sender,
        pilot_gate=_allow_pilot,
        write_back_enabled=True,
    )
    second = resolve_queue_item(
        sqlite_session,
        clinic_id,
        f"booking_action:{booking_action_id}",
        QueueDecision.APPROVE,
        staff_actor="staff:alice",
        now=NOW,
        sender=sender,
        pilot_gate=_allow_pilot,
        write_back_enabled=True,
    )

    action = sqlite_session.get(BookingAction, booking_action_id)
    job = sqlite_session.get(OutreachJob, f"job-{clinic_id}")
    audit_actions = sqlite_session.execute(select(AuditLog.action)).scalars().all()

    assert first.resolved is True
    assert first.booking_status == "completed"
    assert first.confirmation_sent is False
    assert first.provider_confirmed is False
    assert second.idempotent is True
    assert sender.sms_messages == []
    assert action.status == BookingActionStatus.COMPLETED
    assert action.write_back_state == BookingWriteBackState.PENDING
    assert action.approved_by == "staff:alice"
    assert job.state == OutreachState.COMPLETED
    assert AuditAction.APPROVE in audit_actions
    effects = list(
        sqlite_session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            )
        ).scalars()
    )
    assert len(effects) == 1


def test_reject_pending_booking_has_no_booking_side_effects(sqlite_session):
    clinic_id, slot_id = _seed_job(sqlite_session)
    booking_action_id = _pending_booking(sqlite_session, clinic_id, slot_id)
    sender = FakeMessageSender()

    result = resolve_queue_item(
        sqlite_session,
        clinic_id,
        f"booking_action:{booking_action_id}",
        QueueDecision.REJECT,
        staff_actor="staff:alice",
        now=NOW,
        sender=sender,
        pilot_gate=_allow_pilot,
        reason="Patient needs a callback first.",
    )

    action = sqlite_session.get(BookingAction, booking_action_id)
    job = sqlite_session.get(OutreachJob, f"job-{clinic_id}")
    audit_actions = sqlite_session.execute(select(AuditLog.action)).scalars().all()

    assert result.resolved is True
    assert result.booking_status == "rejected"
    assert result.confirmation_sent is False
    assert sender.sms_messages == []
    assert action.status == BookingActionStatus.REJECTED
    assert job.state == OutreachState.COMPLETED
    assert AuditAction.REJECT in audit_actions


def test_approve_pending_booking_rejects_frozen_subject_without_side_effects(
    sqlite_session,
):
    clinic_id, slot_id = _seed_job(sqlite_session)
    booking_action_id = _pending_booking(sqlite_session, clinic_id, slot_id)
    request_patient_erasure(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id=f"patient-{clinic_id}",
        confirm_token=f"ERASE patient-{clinic_id}",
        request_identity="tests-staff-queue-freeze",
        actor_role="dpo",
        actor_reference="tests-staff-queue-operator",
        keyring=SubjectKeyring(
            current=SubjectKey("tests-staff-queue-v1", b"tests-staff-queue-key")
        ),
        policy=RightsPolicy("tests-staff-policy-v1", "a" * 64, timedelta(days=28)),
        now=NOW,
    )
    sender = FakeMessageSender()

    with pytest.raises(SubjectFrozenError, match="subject_frozen"):
        resolve_queue_item(
            sqlite_session,
            clinic_id,
            f"booking_action:{booking_action_id}",
            QueueDecision.APPROVE,
            staff_actor="staff:alice",
            now=NOW,
            sender=sender,
            pilot_gate=_allow_pilot,
        )

    action = sqlite_session.get(BookingAction, booking_action_id)
    assert action is not None and action.status == BookingActionStatus.PENDING
    assert action.approved_by is None
    assert sender.sms_messages == []


def test_staff_queue_is_scoped_to_one_clinic(sqlite_session):
    clinic_id, slot_id = _seed_job(sqlite_session, "clinic-a")
    other_clinic_id, _ = _seed_job(sqlite_session, "clinic-b")
    booking_action_id = _pending_booking(sqlite_session, clinic_id, slot_id)

    assert list_staff_queue(sqlite_session, other_clinic_id) == []
    with pytest.raises(LookupError):
        resolve_queue_item(
            sqlite_session,
            other_clinic_id,
            f"booking_action:{booking_action_id}",
            QueueDecision.APPROVE,
            staff_actor="staff:bob",
            now=NOW,
            sender=FakeMessageSender(),
            pilot_gate=_allow_pilot,
        )