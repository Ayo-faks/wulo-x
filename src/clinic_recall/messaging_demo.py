"""Phase 2 demo: run the deterministic SMS/email loop against a fake sender."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .enums import (
    CampaignStatus,
    CampaignType,
    Channel,
    InteractionDirection,
    InteractionIntent,
    OutreachState,
)
from .messaging.inbound import handle_inbound_reply
from .messaging.orchestrator import run_cadence
from .messaging.send import send_sms
from .messaging.sender import FakeMessageSender
from .models import (
    Appointment,
    AuditLog,
    Base,
    Campaign,
    Clinic,
    Escalation,
    Interaction,
    OutreachJob,
    Patient,
)
from .pilot_controls import PilotGateDecision

NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
CLINIC_ID = "phase2-demo-clinic"


def main() -> int:
    """Run the Phase 2 SMS-loop demo and print a compact summary."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sender = FakeMessageSender()
    with Session(engine, expire_on_commit=False) as session:
        _seed(session)

        cadence = run_cadence(
            session,
            CLINIC_ID,
            NOW,
            sender=sender,
            pilot_gate=_synthetic_demo_pilot_gate,
        )
        opt_out = handle_inbound_reply(
            session,
            clinic_id=CLINIC_ID,
            from_address="+447700910003",
            channel=Channel.SMS,
            body="STOP",
            now=NOW,
        )
        after_opt_out = _add_follow_up_job(
            session,
            "job-after-optout-sms",
            "patient-optout",
            "appointment-optout",
        )
        opt_out_block = send_sms(
            session,
            CLINIC_ID,
            after_opt_out,
            NOW,
            sender,
            pilot_gate=_synthetic_demo_pilot_gate,
        )

        clinical = handle_inbound_reply(
            session,
            clinic_id=CLINIC_ID,
            from_address="+447700910004",
            channel=Channel.SMS,
            body="My knee hurts after treatment",
            now=NOW,
        )
        urgent = handle_inbound_reply(
            session,
            clinic_id=CLINIC_ID,
            from_address="+447700910005",
            channel=Channel.SMS,
            body="urgent chest pain",
            now=NOW,
        )

        cap_job = _add_job(session, "cap-block", "+447700910006", state=OutreachState.QUEUED)
        clinic = session.get(Clinic, CLINIC_ID)
        assert clinic is not None
        clinic.daily_caps = 1
        cap_block = send_sms(
            session,
            CLINIC_ID,
            cap_job,
            NOW,
            sender,
            pilot_gate=_synthetic_demo_pilot_gate,
        )
        session.flush()
        assert opt_out_block.skip_reason is not None
        assert cap_block.skip_reason is not None

        print("Clinic Recall - Phase 2 SMS-loop demo")
        print(f"Cadence: {cadence.as_summary()}")
        print(f"Fake sends: sms={len(sender.sms_messages)} email={len(sender.email_messages)}")
        print(f"Opt-out intent: {opt_out.intent.value}; later send blocked={opt_out_block.skip_reason.value}")
        print(f"Clinical escalated={clinical.escalated}; urgent escalated={urgent.escalated}")
        print(f"Daily-cap blocked={cap_block.skip_reason.value}")
        print(f"OutreachState counts: {_state_counts(session)}")
        print(f"InteractionIntent counts: {_intent_counts(session)}")
        print(f"Escalation counts: {_escalation_counts(session)}")
        print(f"Audit rows: {session.execute(select(AuditLog)).scalars().all().__len__()}")
    engine.dispose()
    return 0


def _synthetic_demo_pilot_gate(*_args) -> PilotGateDecision:
    return PilotGateDecision(True, "synthetic_demo")


def _seed(session: Session) -> None:
    session.add(
        Clinic(
            id=CLINIC_ID,
            name="Phase 2 Demo Clinic",
            sms_number="+447700910000",
            timezone="Europe/London",
            daily_caps=200,
            branding={"booking_url": "https://booking.example.test/phase2"},
        )
    )
    session.add(
        Campaign(
            id="phase2-demo-campaign",
            clinic_id=CLINIC_ID,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    _add_job(session, "queued", "+447700910001", state=OutreachState.QUEUED)
    stale_sms_job = _add_job(session, "stale-sms", "+447700910002", state=OutreachState.SENT)
    session.add(
        Interaction(
            id="interaction-stale-sms-outbound",
            clinic_id=CLINIC_ID,
            outreach_job_id=stale_sms_job,
            channel=Channel.SMS,
            direction=InteractionDirection.OUTBOUND,
            content="older sms",
            occurred_at=NOW - timedelta(hours=49),
        )
    )
    _add_job(session, "optout", "+447700910003", state=OutreachState.SENT)
    _add_job(session, "clinical", "+447700910004", state=OutreachState.SENT)
    _add_job(session, "urgent", "+447700910005", state=OutreachState.SENT)
    session.flush()


def _add_job(session: Session, suffix: str, phone: str, *, state: OutreachState) -> str:
    patient_id = f"patient-{suffix}"
    appointment_id = f"appointment-{suffix}"
    job_id = f"job-{suffix}-sms"
    session.add(
        Patient(
            id=patient_id,
            clinic_id=CLINIC_ID,
            source_ref=patient_id,
            name=f"{suffix.title()} Patient",
            phone=phone,
            email=f"{suffix}@example.test",
            consent_flags={"sms": True, "email": True},
            opt_out_flags={},
        )
    )
    session.add(
        Appointment(
            id=appointment_id,
            clinic_id=CLINIC_ID,
            patient_id=patient_id,
            source_ref=appointment_id,
            status="missed",
            start_at=NOW - timedelta(days=5),
        )
    )
    session.add(
        OutreachJob(
            id=job_id,
            clinic_id=CLINIC_ID,
            campaign_id="phase2-demo-campaign",
            patient_id=patient_id,
            appointment_id=appointment_id,
            channel=Channel.SMS,
            state=state,
        )
    )
    session.flush()
    return job_id


def _add_follow_up_job(
    session: Session,
    job_id: str,
    patient_id: str,
    appointment_id: str,
) -> str:
    session.add(
        OutreachJob(
            id=job_id,
            clinic_id=CLINIC_ID,
            campaign_id="phase2-demo-campaign",
            patient_id=patient_id,
            appointment_id=appointment_id,
            channel=Channel.SMS,
            state=OutreachState.QUEUED,
        )
    )
    session.flush()
    return job_id


def _state_counts(session: Session) -> dict[str, int]:
    rows = session.execute(select(OutreachJob.state)).scalars().all()
    return dict(sorted(Counter(state.value for state in rows).items()))


def _intent_counts(session: Session) -> dict[str, int]:
    rows = session.execute(select(Interaction.intent)).scalars().all()
    counts = Counter(intent.value for intent in rows if isinstance(intent, InteractionIntent))
    return dict(sorted(counts.items()))


def _escalation_counts(session: Session) -> dict[str, int]:
    rows = session.execute(select(Escalation.reason)).scalars().all()
    return dict(sorted(Counter(reason.value for reason in rows).items()))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())