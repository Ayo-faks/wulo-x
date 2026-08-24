"""Phase 5 NFR tests for burst idempotency and cost tracking."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from src.clinic_recall.costs import get_interaction_cost_summary
from src.clinic_recall.durable.worker import run_once as _run_once
from src.clinic_recall.enums import (
    AppointmentStatus,
    CampaignStatus,
    CampaignType,
    Channel,
    InteractionDirection,
    InteractionOutcome,
    OutreachState,
)
from src.clinic_recall.messaging.orchestrator import run_cadence
from src.clinic_recall.messaging.sender import FakeMessageSender
from src.clinic_recall.models import (
    Appointment,
    Campaign,
    Clinic,
    Interaction,
    OutreachJob,
    Patient,
)
from src.clinic_recall.pilot_controls import PilotGateDecision

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _allow_pilot(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "allowed")


def run_once(*args, **kwargs):
    kwargs.setdefault("programme_gate", _allow_pilot)
    return _run_once(*args, **kwargs)


def _seed_sms_burst(sqlite_session, *, count: int = 50) -> str:
    clinic_id = "clinic-nfr"
    sqlite_session.add(
        Clinic(
            id=clinic_id,
            name="NFR Clinic",
            timezone="Europe/London",
            daily_caps=10_000,
        )
    )
    sqlite_session.add(
        Campaign(
            id="campaign-nfr",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    for index in range(count):
        patient_id = f"patient-nfr-{index}"
        appointment_id = f"appointment-nfr-{index}"
        job_id = f"job-nfr-{index}"
        sqlite_session.add(
            Patient(
                id=patient_id,
                clinic_id=clinic_id,
                source_ref=f"P-NFR-{index}",
                name=f"NFR Patient {index}",
                phone=f"+447701{index:06d}",
                email=f"nfr{index}@example.test",
                consent_flags={"sms": True, "email": True, "call": True},
                opt_out_flags={},
            )
        )
        sqlite_session.add(
            Appointment(
                id=appointment_id,
                clinic_id=clinic_id,
                patient_id=patient_id,
                source_ref=f"A-NFR-{index}",
                status=AppointmentStatus.MISSED,
                start_at=NOW,
            )
        )
        sqlite_session.add(
            OutreachJob(
                id=job_id,
                clinic_id=clinic_id,
                campaign_id="campaign-nfr",
                patient_id=patient_id,
                appointment_id=appointment_id,
                channel=Channel.SMS,
                state=OutreachState.QUEUED,
            )
        )
    sqlite_session.flush()
    return clinic_id


def test_sms_burst_drains_without_duplicate_sends(sqlite_session):
    clinic_id = _seed_sms_burst(sqlite_session, count=50)
    sender = FakeMessageSender()

    first = run_cadence(
        sqlite_session, clinic_id, NOW, sender=sender, pilot_gate=_allow_pilot
    )
    second = run_cadence(
        sqlite_session, clinic_id, NOW, sender=sender, pilot_gate=_allow_pilot
    )
    sqlite_session.commit()
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    drained = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-nfr-burst",
        sender=sender,
        now=NOW,
        enabled=True,
        limit=50,
    )
    repeated = run_once(
        factory,
        clinic_id=clinic_id,
        worker_id="worker-nfr-repeat",
        sender=sender,
        now=NOW,
        enabled=True,
        limit=50,
    )
    sqlite_session.expire_all()

    assert first.sms_enqueued == 50
    assert second.sms_enqueued == 0
    assert drained.succeeded == 50
    assert repeated.claimed == 0
    assert len(sender.sms_messages) == 50
    assert sqlite_session.execute(select(func.count()).select_from(Interaction)).scalar() == 50
    assert sqlite_session.execute(
        select(func.count()).select_from(OutreachJob).where(OutreachJob.state == OutreachState.SENT)
    ).scalar() == 50


def test_interaction_cost_summary_counts_sms_before_voice_costs(sqlite_session):
    clinic_id = _seed_sms_burst(sqlite_session, count=2)
    sender = FakeMessageSender()
    planned = run_cadence(
        sqlite_session, clinic_id, NOW, sender=sender, pilot_gate=_allow_pilot
    )
    before_dispatch = get_interaction_cost_summary(
        sqlite_session,
        clinic_id,
        start=NOW,
        end=NOW.replace(hour=13),
    )
    sqlite_session.commit()
    dispatched = run_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="worker-nfr-cost",
        sender=sender,
        now=NOW,
        enabled=True,
        limit=2,
    )
    sqlite_session.add(
        Interaction(
            id="interaction-call-cost",
            clinic_id=clinic_id,
            outreach_job_id="job-nfr-0",
            channel=Channel.CALL,
            direction=InteractionDirection.OUTBOUND,
            content="call-id",
            outcome=InteractionOutcome.AUTO_HANDLED,
            occurred_at=NOW,
        )
    )
    sqlite_session.flush()

    summary = get_interaction_cost_summary(sqlite_session, clinic_id, start=NOW, end=NOW.replace(hour=13))

    assert planned.sms_enqueued == 2
    assert before_dispatch.sms_count == 0
    assert dispatched.succeeded == 2
    assert summary.sms_count == 2
    assert summary.call_count == 1
    assert summary.email_count == 0
    assert summary.total_estimated_cost == Decimal("0.35")