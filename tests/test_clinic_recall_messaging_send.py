"""Tests for Phase 2 deterministic messaging sends."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from src.clinic_recall.enums import (
    AuditAction,
    CampaignStatus,
    CampaignType,
    Channel,
    InteractionDirection,
    OutreachState,
    SkipReason,
)
from src.clinic_recall.messaging.send import send_email as _send_email
from src.clinic_recall.messaging.send import send_sms as _send_sms
from src.clinic_recall.messaging.sender import FakeMessageSender
from src.clinic_recall.models import (
    Appointment,
    AuditLog,
    Campaign,
    Clinic,
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

NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)


def _allow_pilot(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def send_sms(*args, **kwargs):
    kwargs.setdefault("pilot_gate", _allow_pilot)
    return _send_sms(*args, **kwargs)


def send_email(*args, **kwargs):
    kwargs.setdefault("pilot_gate", _allow_pilot)
    return _send_email(*args, **kwargs)


def _seed_sms_job(sqlite_session, *, opt_out_sms: bool = False) -> str:
    clinic_id = "clinic-send"
    sqlite_session.add(
        Clinic(
            id=clinic_id,
            name="Send Clinic",
            sms_number="+447700900000",
            timezone="Europe/London",
            daily_caps=200,
            branding={"booking_url": "https://booking.example.test/send"},
        )
    )
    sqlite_session.add(
        Patient(
            id="patient-send",
            clinic_id=clinic_id,
            source_ref="P-SEND",
            name="Amina Patient",
            phone="+447700900001",
            email="amina@example.test",
            consent_flags={"sms": True, "email": True},
            opt_out_flags={"sms": opt_out_sms},
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-send",
            clinic_id=clinic_id,
            patient_id="patient-send",
            source_ref="A-SEND",
            status="missed",
            start_at=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
        )
    )
    sqlite_session.add(
        Campaign(
            id="campaign-send",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-send-sms",
            clinic_id=clinic_id,
            campaign_id="campaign-send",
            patient_id="patient-send",
            appointment_id="appointment-send",
            channel=Channel.SMS,
            state=OutreachState.QUEUED,
        )
    )
    sqlite_session.flush()
    return clinic_id


def _seed_email_job(sqlite_session) -> str:
    clinic_id = _seed_sms_job(sqlite_session)
    sqlite_session.add(
        OutreachJob(
            id="job-send-email",
            clinic_id=clinic_id,
            campaign_id="campaign-send",
            patient_id="patient-send",
            appointment_id="appointment-send",
            channel=Channel.EMAIL,
            state=OutreachState.QUEUED,
        )
    )
    sqlite_session.flush()
    return clinic_id


def test_send_sms_records_outbound_interaction_and_audit(sqlite_session):
    clinic_id = _seed_sms_job(sqlite_session)
    sender = FakeMessageSender()

    result = send_sms(sqlite_session, clinic_id, "job-send-sms", NOW, sender)

    assert result.sent is True
    assert result.state == OutreachState.SENT
    assert len(sender.sms_messages) == 1
    assert sender.sms_messages[0].to == "+447700900001"
    assert "Reply STOP to opt out." in sender.sms_messages[0].body

    job = sqlite_session.get(OutreachJob, "job-send-sms")
    assert job is not None
    assert job.state == OutreachState.SENT
    assert job.attempts == 1

    interaction = sqlite_session.execute(select(Interaction)).scalar_one()
    assert interaction.channel == Channel.SMS
    assert interaction.direction == InteractionDirection.OUTBOUND
    assert interaction.content == sender.sms_messages[0].body

    audit_actions = sqlite_session.execute(select(AuditLog.action)).scalars().all()
    assert audit_actions == [AuditAction.SEND_SMS]


def test_send_sms_is_idempotent_for_same_job_attempt(sqlite_session):
    clinic_id = _seed_sms_job(sqlite_session)
    sender = FakeMessageSender()

    first = send_sms(sqlite_session, clinic_id, "job-send-sms", NOW, sender)
    second = send_sms(sqlite_session, clinic_id, "job-send-sms", NOW, sender)

    assert first.sent is True
    assert second.sent is False
    assert second.idempotent is True
    assert len(sender.sms_messages) == 1
    assert sqlite_session.execute(select(func.count()).select_from(Interaction)).scalar() == 1


def test_send_sms_cancels_before_provider_io_when_subject_frozen(sqlite_session):
    clinic_id = _seed_sms_job(sqlite_session)
    request_patient_erasure(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id="patient-send",
        confirm_token="ERASE patient-send",
        request_identity="tests-send-freeze",
        actor_role="dpo",
        actor_reference="tests-send-operator",
        keyring=SubjectKeyring(
            current=SubjectKey(
                version="tests-send-v1",
                secret=b"tests-send-rights-key-material",
            )
        ),
        policy=RightsPolicy(
            version="tests-send-policy-v1",
            approval_evidence_hash="a" * 64,
            request_due_after=timedelta(days=28),
        ),
        now=NOW,
    )
    sender = FakeMessageSender()

    result = send_sms(sqlite_session, clinic_id, "job-send-sms", NOW, sender)

    assert result.sent is False
    assert result.error == "subject_frozen"
    assert result.state == OutreachState.QUEUED
    assert sender.sms_messages == []
    assert sqlite_session.execute(select(func.count()).select_from(Interaction)).scalar() == 0


def test_send_sms_rechecks_opt_out_at_send_time(sqlite_session):
    clinic_id = _seed_sms_job(sqlite_session, opt_out_sms=True)
    sender = FakeMessageSender()

    result = send_sms(sqlite_session, clinic_id, "job-send-sms", NOW, sender)

    assert result.sent is False
    assert result.skip_reason == SkipReason.OPTED_OUT
    assert result.state == OutreachState.FAILED
    assert len(sender.sms_messages) == 0
    assert sqlite_session.execute(select(func.count()).select_from(Interaction)).scalar() == 0


def test_send_sms_defers_outside_contact_hours_at_send_time(sqlite_session):
    clinic_id = _seed_sms_job(sqlite_session)
    sender = FakeMessageSender()
    outside_hours = datetime(2026, 6, 26, 22, 0, tzinfo=UTC)

    result = send_sms(sqlite_session, clinic_id, "job-send-sms", outside_hours, sender)

    job = sqlite_session.get(OutreachJob, "job-send-sms")
    assert result.sent is False
    assert result.skip_reason == SkipReason.OUTSIDE_CONTACT_HOURS
    assert result.state == OutreachState.QUEUED
    assert job is not None
    assert job.state == OutreachState.QUEUED
    assert job.next_action_at is not None
    assert len(sender.sms_messages) == 0


def test_send_sms_rechecks_daily_cap_from_outbound_interactions(sqlite_session):
    clinic_id = _seed_sms_job(sqlite_session)
    clinic = sqlite_session.get(Clinic, clinic_id)
    assert clinic is not None
    clinic.daily_caps = 1
    sqlite_session.add(
        Interaction(
            id="interaction-existing-outbound",
            clinic_id=clinic_id,
            outreach_job_id="job-send-sms",
            channel=Channel.EMAIL,
            direction=InteractionDirection.OUTBOUND,
            content="already contacted today",
            occurred_at=NOW,
        )
    )
    sqlite_session.flush()
    sender = FakeMessageSender()

    result = send_sms(sqlite_session, clinic_id, "job-send-sms", NOW, sender)

    assert result.sent is False
    assert result.skip_reason == SkipReason.DAILY_CAP
    assert result.state == OutreachState.QUEUED
    assert len(sender.sms_messages) == 0


def test_send_email_records_outbound_interaction_and_audit(sqlite_session):
    clinic_id = _seed_email_job(sqlite_session)
    sender = FakeMessageSender()

    result = send_email(sqlite_session, clinic_id, "job-send-email", NOW, sender)

    assert result.sent is True
    assert result.state == OutreachState.SENT
    assert len(sender.email_messages) == 1
    assert sender.email_messages[0].to == "amina@example.test"
    assert sender.email_messages[0].subject.startswith("Rebook your appointment")

    interactions = sqlite_session.execute(
        select(Interaction).where(Interaction.outreach_job_id == "job-send-email")
    ).scalars().all()
    assert len(interactions) == 1
    assert interactions[0].channel == Channel.EMAIL
    assert interactions[0].direction == InteractionDirection.OUTBOUND

    email_audit = sqlite_session.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.SEND_EMAIL)
    ).scalar_one()
    assert email_audit.entity_ref == "job-send-email"


def test_send_sms_does_not_cross_clinic_scope(sqlite_session):
    _seed_sms_job(sqlite_session)
    sqlite_session.add(
        Clinic(
            id="clinic-other",
            name="Other Clinic",
            sms_number="+447700900099",
            timezone="Europe/London",
            daily_caps=200,
        )
    )
    sqlite_session.flush()

    sender = FakeMessageSender()
    try:
        send_sms(sqlite_session, "clinic-other", "job-send-sms", NOW, sender)
    except LookupError:
        pass
    else:  # pragma: no cover - the assertion below is clearer when this path happens
        raise AssertionError("cross-clinic send unexpectedly found another clinic's job")

    assert len(sender.sms_messages) == 0