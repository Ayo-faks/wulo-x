"""Tests for Phase 5 deterministic post-visit feedback handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from src.clinic_recall.enums import (
    AppointmentStatus,
    AuditAction,
    CampaignStatus,
    CampaignType,
    Channel,
    EscalationPriority,
    EscalationReason,
    InteractionIntent,
    InteractionOutcome,
    OutreachState,
)
from src.clinic_recall.feedback import generate_feedback_queue
from src.clinic_recall.messaging.inbound import handle_inbound_reply
from src.clinic_recall.messaging.send import send_sms as _send_sms
from src.clinic_recall.messaging.sender import FakeMessageSender
from src.clinic_recall.models import (
    Appointment,
    AuditLog,
    Campaign,
    Clinic,
    Escalation,
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

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _allow_pilot(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def send_sms(*args, **kwargs):
    kwargs.setdefault("pilot_gate", _allow_pilot)
    return _send_sms(*args, **kwargs)


def _seed_clinic(sqlite_session) -> str:
    clinic_id = "clinic-feedback"
    sqlite_session.add(
        Clinic(
            id=clinic_id,
            name="Feedback Clinic",
            sms_number="+447700900100",
            timezone="Europe/London",
            daily_caps=200,
        )
    )
    sqlite_session.add(
        Patient(
            id="patient-feedback",
            clinic_id=clinic_id,
            source_ref="P-FEEDBACK",
            name="Ada Patient",
            phone="+447700900101",
            email="ada@example.test",
            consent_flags={"sms": True, "email": True},
            opt_out_flags={},
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-completed",
            clinic_id=clinic_id,
            patient_id="patient-feedback",
            source_ref="A-COMPLETE",
            status=AppointmentStatus.COMPLETED,
            start_at=datetime(2026, 6, 25, 9, 0, tzinfo=UTC),
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-missed",
            clinic_id=clinic_id,
            patient_id="patient-feedback",
            source_ref="A-MISSED",
            status=AppointmentStatus.MISSED,
            start_at=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
        )
    )
    sqlite_session.flush()
    return clinic_id


def _seed_feedback_job(sqlite_session, *, state: OutreachState = OutreachState.SENT) -> str:
    clinic_id = _seed_clinic(sqlite_session)
    sqlite_session.add(
        Campaign(
            id="campaign-feedback",
            clinic_id=clinic_id,
            type=CampaignType.FEEDBACK,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-feedback-sms",
            clinic_id=clinic_id,
            campaign_id="campaign-feedback",
            patient_id="patient-feedback",
            appointment_id="appointment-completed",
            channel=Channel.SMS,
            state=state,
        )
    )
    sqlite_session.flush()
    return clinic_id


def test_feedback_queue_targets_completed_visits_and_is_idempotent(sqlite_session):
    clinic_id = _seed_clinic(sqlite_session)

    first = generate_feedback_queue(
        sqlite_session,
        clinic_id,
        NOW,
        pilot_gate=_allow_pilot,
    )
    second = generate_feedback_queue(
        sqlite_session,
        clinic_id,
        NOW,
        pilot_gate=_allow_pilot,
    )

    assert first.detected == 1
    assert first.queued == 1
    assert second.queued == 0
    assert second.already_queued == 1

    campaign = sqlite_session.execute(select(Campaign)).scalar_one()
    assert campaign.type == CampaignType.FEEDBACK

    job = sqlite_session.execute(select(OutreachJob)).scalar_one()
    assert job.appointment_id == "appointment-completed"
    assert job.channel == Channel.SMS
    assert job.state == OutreachState.QUEUED


def test_feedback_queue_skips_frozen_subject(sqlite_session):
    clinic_id = _seed_clinic(sqlite_session)
    request_patient_erasure(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id="patient-feedback",
        confirm_token="ERASE patient-feedback",
        request_identity="tests-feedback-freeze",
        actor_role="dpo",
        actor_reference="tests-feedback-operator",
        keyring=SubjectKeyring(
            current=SubjectKey("tests-feedback-v1", b"tests-feedback-freeze-key")
        ),
        policy=RightsPolicy("tests-feedback-policy-v1", "a" * 64, timedelta(days=28)),
        now=NOW,
    )

    result = generate_feedback_queue(
        sqlite_session,
        clinic_id,
        NOW,
        pilot_gate=_allow_pilot,
    )

    assert result.queued == 0
    assert result.skipped == {"subject_frozen": 1}
    assert sqlite_session.execute(select(func.count()).select_from(OutreachJob)).scalar() == 0


def test_send_feedback_sms_uses_feedback_template_and_is_idempotent(sqlite_session):
    clinic_id = _seed_feedback_job(sqlite_session, state=OutreachState.QUEUED)
    sender = FakeMessageSender()

    first = send_sms(sqlite_session, clinic_id, "job-feedback-sms", NOW, sender)
    second = send_sms(sqlite_session, clinic_id, "job-feedback-sms", NOW, sender)

    assert first.sent is True
    assert second.idempotent is True
    assert len(sender.sms_messages) == 1
    body = sender.sms_messages[0].body.lower()
    assert "rate" in body
    assert "1-5" in body
    assert "we missed you" not in body

    assert sqlite_session.execute(select(AuditLog.action)).scalars().all() == [
        AuditAction.SEND_SMS
    ]


def test_positive_feedback_is_recorded_and_completed_without_staff_escalation(sqlite_session):
    clinic_id = _seed_feedback_job(sqlite_session)

    result = handle_inbound_reply(
        sqlite_session,
        clinic_id=clinic_id,
        from_address="+447700900101",
        channel=Channel.SMS,
        body="5 - great visit, thank you",
        now=NOW,
    )

    job = sqlite_session.get(OutreachJob, "job-feedback-sms")
    interaction = sqlite_session.execute(select(Interaction)).scalar_one()

    assert result.intent == InteractionIntent.FEEDBACK
    assert result.escalated is False
    assert job.state == OutreachState.COMPLETED
    assert interaction.intent == InteractionIntent.FEEDBACK
    assert interaction.outcome == InteractionOutcome.AUTO_HANDLED
    assert sqlite_session.execute(select(func.count()).select_from(Escalation)).scalar() == 0
    assert sqlite_session.execute(select(AuditLog.action)).scalars().all() == [
        AuditAction.RECORD_FEEDBACK
    ]


def test_negative_feedback_escalates_as_high_priority_complaint(sqlite_session):
    clinic_id = _seed_feedback_job(sqlite_session)

    result = handle_inbound_reply(
        sqlite_session,
        clinic_id=clinic_id,
        from_address="+447700900101",
        channel=Channel.SMS,
        body="1 - this was awful, I want to complain",
        now=NOW,
    )

    job = sqlite_session.get(OutreachJob, "job-feedback-sms")
    escalation = sqlite_session.execute(select(Escalation)).scalar_one()
    interaction = sqlite_session.execute(select(Interaction)).scalar_one()

    assert result.intent == InteractionIntent.FEEDBACK
    assert result.escalated is True
    assert job.state == OutreachState.ESCALATED
    assert interaction.outcome == InteractionOutcome.ROUTED_TO_STAFF
    assert escalation.reason == EscalationReason.COMPLAINT
    assert escalation.priority == EscalationPriority.HIGH
    assert set(sqlite_session.execute(select(AuditLog.action)).scalars().all()) == {
        AuditAction.RECORD_FEEDBACK,
        AuditAction.ESCALATE,
    }


def test_clinical_feedback_escalates_clinical_and_never_auto_answers(sqlite_session):
    clinic_id = _seed_feedback_job(sqlite_session)

    result = handle_inbound_reply(
        sqlite_session,
        clinic_id=clinic_id,
        from_address="+447700900101",
        channel=Channel.SMS,
        body="5, but my knee hurts after treatment",
        now=NOW,
    )

    escalation = sqlite_session.execute(select(Escalation)).scalar_one()
    interaction = sqlite_session.execute(select(Interaction)).scalar_one()

    assert result.intent == InteractionIntent.CLINICAL
    assert result.escalated is True
    assert interaction.outcome == InteractionOutcome.ROUTED_TO_STAFF
    assert escalation.reason == EscalationReason.CLINICAL
    assert escalation.priority == EscalationPriority.HIGH