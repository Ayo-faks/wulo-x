"""Tests for the Phase 2 campaign cadence orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from src.clinic_recall.enums import (
    CampaignStatus,
    CampaignType,
    Channel,
    ExternalEffectState,
    ExternalEffectType,
    InteractionDirection,
    OutreachState,
)
from src.clinic_recall.messaging.orchestrator import run_cadence
from src.clinic_recall.messaging.sender import FakeMessageSender
from src.clinic_recall.models import (
    Appointment,
    Campaign,
    Clinic,
    ExternalEffect,
    Interaction,
    OutreachJob,
    Patient,
)
from src.clinic_recall.pilot_controls import PilotGateDecision

NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)


class _FailIfCalledSender:
    name = "fail-if-called"

    def send_sms(self, **_kwargs):
        raise AssertionError("cadence must not call the SMS provider boundary")

    def send_email(self, **_kwargs):
        raise AssertionError("cadence must not call the email provider boundary")


def _allow_pilot(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def _seed_base(sqlite_session) -> str:
    clinic_id = "clinic-cadence"
    sqlite_session.add(
        Clinic(
            id=clinic_id,
            name="Cadence Clinic",
            sms_number="+447700900000",
            timezone="Europe/London",
            daily_caps=200,
            branding={"booking_url": "https://booking.example.test/cadence"},
        )
    )
    sqlite_session.add(
        Patient(
            id="patient-cadence",
            clinic_id=clinic_id,
            source_ref="P-CADENCE",
            name="Bola Patient",
            phone="+447700900001",
            email="bola@example.test",
            consent_flags={"sms": True, "email": True},
            opt_out_flags={},
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-cadence",
            clinic_id=clinic_id,
            patient_id="patient-cadence",
            source_ref="A-CADENCE",
            status="missed",
            start_at=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
        )
    )
    sqlite_session.add(
        Campaign(
            id="campaign-cadence",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.flush()
    return clinic_id


def _add_job(sqlite_session, *, channel: Channel, state: OutreachState, job_id: str) -> None:
    sqlite_session.add(
        OutreachJob(
            id=job_id,
            clinic_id="clinic-cadence",
            campaign_id="campaign-cadence",
            patient_id="patient-cadence",
            appointment_id="appointment-cadence",
            channel=channel,
            state=state,
        )
    )
    sqlite_session.flush()


def test_run_cadence_enqueues_queued_sms_without_provider_call(sqlite_session):
    clinic_id = _seed_base(sqlite_session)
    _add_job(sqlite_session, channel=Channel.SMS, state=OutreachState.QUEUED, job_id="job-sms")
    sender = _FailIfCalledSender()

    first = run_cadence(
        sqlite_session, clinic_id, NOW, sender=sender, pilot_gate=_allow_pilot
    )
    second = run_cadence(
        sqlite_session, clinic_id, NOW, sender=sender, pilot_gate=_allow_pilot
    )

    effects = sqlite_session.execute(
        select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.SMS)
    ).scalars().all()
    assert first.sms_enqueued == 1
    assert second.sms_enqueued == 0
    assert len(effects) == 1
    assert effects[0].state == ExternalEffectState.PENDING
    assert effects[0].payload == {"intent": "recall", "outreach_job_id": "job-sms"}
    assert sqlite_session.get(OutreachJob, "job-sms").state == OutreachState.QUEUED


def test_run_cadence_does_not_send_draft_campaign_jobs(sqlite_session):
    clinic_id = _seed_base(sqlite_session)
    sqlite_session.get(Campaign, "campaign-cadence").status = CampaignStatus.DRAFT
    _add_job(sqlite_session, channel=Channel.SMS, state=OutreachState.QUEUED, job_id="job-sms")
    sender = FakeMessageSender()

    result = run_cadence(
        sqlite_session, clinic_id, NOW, sender=sender, pilot_gate=_allow_pilot
    )

    assert result.sms_sent == 0
    assert len(sender.sms_messages) == 0
    assert sqlite_session.get(OutreachJob, "job-sms").state == OutreachState.QUEUED


def test_run_cadence_does_not_enqueue_paused_campaign_jobs(sqlite_session):
    clinic_id = _seed_base(sqlite_session)
    sqlite_session.get(Campaign, "campaign-cadence").status = CampaignStatus.PAUSED
    _add_job(sqlite_session, channel=Channel.SMS, state=OutreachState.QUEUED, job_id="job-sms")

    result = run_cadence(
        sqlite_session,
        clinic_id,
        NOW,
        sender=_FailIfCalledSender(),
        pilot_gate=_allow_pilot,
    )

    assert result.sms_enqueued == 0
    assert sqlite_session.execute(select(ExternalEffect)).scalars().all() == []
    assert sqlite_session.get(OutreachJob, "job-sms").state == OutreachState.QUEUED


def test_run_cadence_does_not_derive_voice_clock_from_interaction(sqlite_session):
    clinic_id = _seed_base(sqlite_session)
    _add_job(sqlite_session, channel=Channel.SMS, state=OutreachState.SENT, job_id="job-sms")
    sqlite_session.add(
        Interaction(
            id="interaction-sms-outbound",
            clinic_id=clinic_id,
            outreach_job_id="job-sms",
            channel=Channel.SMS,
            direction=InteractionDirection.OUTBOUND,
            content="sms body",
            occurred_at=NOW - timedelta(hours=49),
        )
    )
    sqlite_session.flush()
    sender = _FailIfCalledSender()

    result = run_cadence(
        sqlite_session, clinic_id, NOW, sender=sender, pilot_gate=_allow_pilot
    )

    assert result.sms_no_reply == 0
    assert result.email_sent == 0
    sms_job = sqlite_session.get(OutreachJob, "job-sms")
    assert sms_job.state == OutreachState.SENT
    email_jobs = sqlite_session.execute(
        select(OutreachJob).where(OutreachJob.channel == Channel.EMAIL)
    ).scalars().all()
    assert email_jobs == []
    assert sqlite_session.execute(select(ExternalEffect)).scalars().all() == []


def test_run_cadence_does_not_advance_fallback_for_paused_campaign(sqlite_session):
    clinic_id = _seed_base(sqlite_session)
    sqlite_session.get(Campaign, "campaign-cadence").status = CampaignStatus.PAUSED
    _add_job(sqlite_session, channel=Channel.SMS, state=OutreachState.SENT, job_id="job-sms")
    sqlite_session.add(
        Interaction(
            id="interaction-sms-outbound",
            clinic_id=clinic_id,
            outreach_job_id="job-sms",
            channel=Channel.SMS,
            direction=InteractionDirection.OUTBOUND,
            content="sms body",
            occurred_at=NOW - timedelta(hours=49),
        )
    )
    sqlite_session.flush()
    sender = _FailIfCalledSender()

    result = run_cadence(
        sqlite_session, clinic_id, NOW, sender=sender, pilot_gate=_allow_pilot
    )

    assert result.sms_no_reply == 0
    assert result.email_sent == 0
    assert sqlite_session.get(OutreachJob, "job-sms").state == OutreachState.SENT
    email_jobs = sqlite_session.execute(
        select(OutreachJob).where(OutreachJob.channel == Channel.EMAIL)
    ).scalars().all()
    assert email_jobs == []


def test_run_cadence_policy_excludes_existing_queued_email(sqlite_session):
    clinic_id = _seed_base(sqlite_session)
    _add_job(sqlite_session, channel=Channel.EMAIL, state=OutreachState.QUEUED, job_id="job-email")

    result = run_cadence(
        sqlite_session,
        clinic_id,
        NOW,
        sender=_FailIfCalledSender(),
        pilot_gate=_allow_pilot,
    )

    email_job = sqlite_session.get(OutreachJob, "job-email")
    assert email_job.state == OutreachState.FAILED
    assert email_job.next_action_at is None
    assert result.email_policy_excluded == 1
    assert result.skipped["patient_email_cadence_disabled"] == 1
    assert sqlite_session.execute(select(ExternalEffect)).scalars().all() == []


def test_run_cadence_policy_excludes_queued_email_for_paused_campaign(sqlite_session):
    clinic_id = _seed_base(sqlite_session)
    sqlite_session.get(Campaign, "campaign-cadence").status = CampaignStatus.PAUSED
    _add_job(sqlite_session, channel=Channel.EMAIL, state=OutreachState.QUEUED, job_id="job-email")

    result = run_cadence(
        sqlite_session,
        clinic_id,
        NOW,
        sender=_FailIfCalledSender(),
        pilot_gate=_allow_pilot,
    )

    assert sqlite_session.get(OutreachJob, "job-email").state == OutreachState.FAILED
    assert result.email_policy_excluded == 1
    assert result.skipped["patient_email_cadence_disabled"] == 1
    assert sqlite_session.execute(select(ExternalEffect)).scalars().all() == []


def test_run_cadence_does_not_advance_historical_sent_email(sqlite_session):
    clinic_id = _seed_base(sqlite_session)
    _add_job(sqlite_session, channel=Channel.EMAIL, state=OutreachState.SENT, job_id="job-email")
    sqlite_session.add(
        Interaction(
            id="interaction-email-outbound",
            clinic_id=clinic_id,
            outreach_job_id="job-email",
            channel=Channel.EMAIL,
            direction=InteractionDirection.OUTBOUND,
            content="email body",
            occurred_at=NOW - timedelta(hours=49),
        )
    )
    sqlite_session.flush()

    result = run_cadence(
        sqlite_session,
        clinic_id,
        NOW,
        sender=_FailIfCalledSender(),
        pilot_gate=_allow_pilot,
    )

    assert result.email_no_reply == 0
    assert result.email_policy_excluded == 0
    assert sqlite_session.get(OutreachJob, "job-email").state == OutreachState.SENT