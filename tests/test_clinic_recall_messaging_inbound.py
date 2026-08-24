"""Tests for deterministic inbound SMS reply handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from src.clinic_recall.enums import (
    AuditAction,
    CampaignStatus,
    CampaignType,
    Channel,
    EscalationPriority,
    EscalationReason,
    InteractionDirection,
    InteractionIntent,
    InteractionOutcome,
    OutreachState,
)
from src.clinic_recall.messaging.inbound import classify_intent, handle_inbound_reply
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
    SubjectFrozenError,
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


def _seed_inbound_job(sqlite_session) -> str:
    clinic_id = "clinic-inbound"
    sqlite_session.add(
        Clinic(
            id=clinic_id,
            name="Inbound Clinic",
            sms_number="+447700900000",
            timezone="Europe/London",
            daily_caps=200,
        )
    )
    sqlite_session.add(
        Patient(
            id="patient-inbound",
            clinic_id=clinic_id,
            source_ref="P-INBOUND",
            name="Chidi Patient",
            phone="+447700900001",
            email="chidi@example.test",
            consent_flags={"sms": True, "email": True},
            opt_out_flags={},
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-inbound",
            clinic_id=clinic_id,
            patient_id="patient-inbound",
            source_ref="A-INBOUND",
            status="missed",
            start_at=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
        )
    )
    sqlite_session.add(
        Campaign(
            id="campaign-inbound",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-inbound-sms",
            clinic_id=clinic_id,
            campaign_id="campaign-inbound",
            patient_id="patient-inbound",
            appointment_id="appointment-inbound",
            channel=Channel.SMS,
            state=OutreachState.SENT,
        )
    )
    sqlite_session.flush()
    return clinic_id


def test_classify_intent_truth_table():
    cases = {
        "yes please book me in": InteractionIntent.REBOOK,
        "need an appointment asap": InteractionIntent.REBOOK,
        "need an interview asap": InteractionIntent.REBOOK,
        "schedule an interview": InteractionIntent.REBOOK,
        "i want to schedule a visit": InteractionIntent.REBOOK,
        "consultation asap": InteractionIntent.REBOOK,
        "no thanks": InteractionIntent.DECLINE,
        "STOP": InteractionIntent.OPT_OUT,
        "what time do you have?": InteractionIntent.QUESTION,
        "what does this appointment involve?": InteractionIntent.QUESTION,
        "my knee hurts after treatment": InteractionIntent.CLINICAL,
        "i want to book an appointment, i have cough and rashes": InteractionIntent.CLINICAL,
        "i have high blood pressure and i also need to arrange an appointment": InteractionIntent.CLINICAL,
        "i want to book and appointment, i have cugh and rashes": InteractionIntent.CLINICAL,
        "urgent chest pain call me": InteractionIntent.URGENT,
        "maybe later-ish": InteractionIntent.UNCLEAR,
    }

    for text, expected in cases.items():
        assert classify_intent(text) == expected


def test_classify_intent_fails_closed_on_mixed_opt_out_clinical():
    assert classify_intent("stop, I have chest pain") == InteractionIntent.URGENT


def test_handle_inbound_reply_records_opt_out_immediately(sqlite_session):
    clinic_id = _seed_inbound_job(sqlite_session)

    result = handle_inbound_reply(
        sqlite_session,
        clinic_id=clinic_id,
        from_address="+447700900001",
        channel=Channel.SMS,
        body="STOP",
        now=NOW,
    )

    patient = sqlite_session.get(Patient, "patient-inbound")
    job = sqlite_session.get(OutreachJob, "job-inbound-sms")
    assert result.intent == InteractionIntent.OPT_OUT
    assert result.escalated is False
    assert patient.opt_out_flags["sms"] is True
    assert job.state == OutreachState.COMPLETED

    interaction = sqlite_session.execute(select(Interaction)).scalar_one()
    assert interaction.direction == InteractionDirection.INBOUND
    assert interaction.intent == InteractionIntent.OPT_OUT
    assert interaction.outcome == InteractionOutcome.AUTO_HANDLED

    assert sqlite_session.execute(select(AuditLog.action)).scalars().all() == [
        AuditAction.OPT_OUT_PATIENT
    ]


def test_handle_inbound_reply_escalates_clinical_and_never_answers(sqlite_session):
    clinic_id = _seed_inbound_job(sqlite_session)

    result = handle_inbound_reply(
        sqlite_session,
        clinic_id=clinic_id,
        from_address="+447700900001",
        channel=Channel.SMS,
        body="My knee hurts and I feel dizzy",
        now=NOW,
    )

    job = sqlite_session.get(OutreachJob, "job-inbound-sms")
    escalation = sqlite_session.execute(select(Escalation)).scalar_one()
    interaction = sqlite_session.execute(select(Interaction)).scalar_one()

    assert result.intent == InteractionIntent.CLINICAL
    assert result.escalated is True
    assert job.state == OutreachState.ESCALATED
    assert escalation.reason == EscalationReason.CLINICAL
    assert escalation.priority == EscalationPriority.HIGH
    assert interaction.outcome == InteractionOutcome.ROUTED_TO_STAFF
    assert sqlite_session.execute(select(AuditLog.action)).scalars().all() == [AuditAction.ESCALATE]


def test_handle_inbound_reply_escalates_urgent_as_high_priority(sqlite_session):
    clinic_id = _seed_inbound_job(sqlite_session)

    result = handle_inbound_reply(
        sqlite_session,
        clinic_id=clinic_id,
        from_address="+447700900001",
        channel=Channel.SMS,
        body="urgent chest pain",
        now=NOW,
    )

    escalation = sqlite_session.execute(select(Escalation)).scalar_one()
    assert result.intent == InteractionIntent.URGENT
    assert result.escalated is True
    assert escalation.reason == EscalationReason.URGENT
    assert escalation.priority == EscalationPriority.HIGH


def test_handle_inbound_reply_escalates_question_as_ambiguous(sqlite_session):
    clinic_id = _seed_inbound_job(sqlite_session)

    result = handle_inbound_reply(
        sqlite_session,
        clinic_id=clinic_id,
        from_address="+447700900001",
        channel=Channel.SMS,
        body="what does this appointment involve?",
        now=NOW,
    )

    escalation = sqlite_session.execute(select(Escalation)).scalar_one()
    assert result.intent == InteractionIntent.QUESTION
    assert result.escalated is True
    assert escalation.reason == EscalationReason.AMBIGUOUS
    assert escalation.priority == EscalationPriority.NORMAL


def test_handle_inbound_reply_marks_rebook_as_replied(sqlite_session):
    clinic_id = _seed_inbound_job(sqlite_session)

    result = handle_inbound_reply(
        sqlite_session,
        clinic_id=clinic_id,
        from_address="+447700900001",
        channel=Channel.SMS,
        body="yes please rebook me",
        now=NOW,
    )

    job = sqlite_session.get(OutreachJob, "job-inbound-sms")
    interaction = sqlite_session.execute(select(Interaction)).scalar_one()

    assert result.intent == InteractionIntent.REBOOK
    assert result.escalated is False
    assert job.state == OutreachState.REPLIED
    assert interaction.outcome == InteractionOutcome.AUTO_HANDLED


def test_handle_inbound_reply_rejects_frozen_subject_before_content_write(
    sqlite_session,
):
    clinic_id = _seed_inbound_job(sqlite_session)
    request_patient_erasure(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id="patient-inbound",
        confirm_token="ERASE patient-inbound",
        request_identity="tests-inbound-freeze",
        actor_role="dpo",
        actor_reference="tests-inbound-operator",
        keyring=SubjectKeyring(
            current=SubjectKey("tests-inbound-v1", b"tests-inbound-freeze-key")
        ),
        policy=RightsPolicy("tests-inbound-policy-v1", "a" * 64, timedelta(days=28)),
        now=NOW,
    )

    with pytest.raises(SubjectFrozenError, match="subject_frozen"):
        handle_inbound_reply(
            sqlite_session,
            clinic_id=clinic_id,
            from_address="+447700900001",
            channel=Channel.SMS,
            body="urgent chest pain",
            now=NOW,
        )

    assert sqlite_session.execute(select(Interaction)).scalars().all() == []
    assert sqlite_session.execute(select(Escalation)).scalars().all() == []


def test_opt_out_blocks_later_send_on_same_channel(sqlite_session):
    clinic_id = _seed_inbound_job(sqlite_session)
    handle_inbound_reply(
        sqlite_session,
        clinic_id=clinic_id,
        from_address="+447700900001",
        channel=Channel.SMS,
        body="STOP",
        now=NOW,
    )
    sqlite_session.add(
        OutreachJob(
            id="job-after-opt-out",
            clinic_id=clinic_id,
            campaign_id="campaign-inbound",
            patient_id="patient-inbound",
            appointment_id="appointment-inbound",
            channel=Channel.SMS,
            state=OutreachState.QUEUED,
        )
    )
    sqlite_session.flush()
    sender = FakeMessageSender()

    result = send_sms(sqlite_session, clinic_id, "job-after-opt-out", NOW, sender)

    assert result.sent is False
    assert len(sender.sms_messages) == 0
    assert sqlite_session.get(OutreachJob, "job-after-opt-out").state == OutreachState.FAILED


def test_handle_inbound_reply_does_not_cross_clinic_scope(sqlite_session):
    _seed_inbound_job(sqlite_session)
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

    try:
        handle_inbound_reply(
            sqlite_session,
            clinic_id="clinic-other",
            from_address="+447700900001",
            channel=Channel.SMS,
            body="STOP",
            now=NOW,
        )
    except LookupError:
        pass
    else:  # pragma: no cover
        raise AssertionError("cross-clinic inbound unexpectedly matched another clinic's patient")

    patient = sqlite_session.get(Patient, "patient-inbound")
    assert patient.opt_out_flags == {}