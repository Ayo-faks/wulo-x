"""Tests for Phase 4 ROI dashboard read models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.clinic_recall.enums import (
    AppointmentStatus,
    BookingActionStatus,
    BookingActionType,
    CampaignStatus,
    CampaignType,
    Channel,
    InteractionDirection,
    InteractionIntent,
    InteractionOutcome,
    OutreachState,
)
from src.clinic_recall.models import (
    Appointment,
    BookingAction,
    Campaign,
    Clinic,
    Interaction,
    OutreachJob,
    Patient,
)
from src.clinic_recall.roi import get_roi_metrics, roi_metrics_csv

START = datetime(2026, 6, 1, tzinfo=UTC)
END = datetime(2026, 7, 1, tzinfo=UTC)


def _seed_clinic(session, clinic_id: str) -> None:
    session.add(
        Clinic(
            id=clinic_id,
            name=f"{clinic_id} Clinic",
            timezone="Europe/London",
            daily_caps=200,
        )
    )
    session.add(
        Patient(
            id=f"patient-{clinic_id}",
            clinic_id=clinic_id,
            source_ref=f"P-{clinic_id}",
            name="ROI Patient",
            phone="+447700910020",
            email="roi@example.test",
            consent_flags={"sms": True, "email": True, "call": True},
            opt_out_flags={},
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
    for index in range(1, 4):
        session.add(
            Appointment(
                id=f"appointment-{clinic_id}-{index}",
                clinic_id=clinic_id,
                patient_id=f"patient-{clinic_id}",
                source_ref=f"A-{clinic_id}-{index}",
                status=AppointmentStatus.COMPLETED,
                start_at=START + timedelta(days=index),
                value=Decimal("75.00") if index == 1 else Decimal("50.00"),
            )
        )
        session.add(
            OutreachJob(
                id=f"job-{clinic_id}-{index}",
                clinic_id=clinic_id,
                campaign_id=f"campaign-{clinic_id}",
                patient_id=f"patient-{clinic_id}",
                appointment_id=f"appointment-{clinic_id}-{index}",
                channel=Channel.SMS,
                state=OutreachState.COMPLETED if index == 1 else OutreachState.SENT,
            )
        )
    session.flush()


def test_roi_metrics_are_aggregate_only_and_formula_based(sqlite_session):
    _seed_clinic(sqlite_session, "clinic-roi")
    sqlite_session.add_all(
        [
            Interaction(
                id="interaction-contact-1",
                clinic_id="clinic-roi",
                outreach_job_id="job-clinic-roi-1",
                channel=Channel.SMS,
                direction=InteractionDirection.OUTBOUND,
                content="PII body must not appear",
                outcome=InteractionOutcome.AUTO_HANDLED,
                occurred_at=START + timedelta(days=2),
            ),
            Interaction(
                id="interaction-contact-2",
                clinic_id="clinic-roi",
                outreach_job_id="job-clinic-roi-2",
                channel=Channel.EMAIL,
                direction=InteractionDirection.OUTBOUND,
                content="Another private body",
                outcome=InteractionOutcome.AUTO_HANDLED,
                occurred_at=START + timedelta(days=3),
            ),
            Interaction(
                id="interaction-opt-out",
                clinic_id="clinic-roi",
                outreach_job_id="job-clinic-roi-2",
                channel=Channel.SMS,
                direction=InteractionDirection.INBOUND,
                content="STOP",
                intent=InteractionIntent.OPT_OUT,
                outcome=InteractionOutcome.AUTO_HANDLED,
                occurred_at=START + timedelta(days=4),
            ),
            BookingAction(
                id="booking-action-roi",
                clinic_id="clinic-roi",
                appointment_id="appointment-clinic-roi-1",
                outreach_job_id="job-clinic-roi-1",
                type=BookingActionType.BOOK,
                status=BookingActionStatus.COMPLETED,
                created_at=START + timedelta(days=5),
                updated_at=START + timedelta(days=5),
            ),
            Appointment(
                id="previous-no-show",
                clinic_id="clinic-roi",
                patient_id="patient-clinic-roi",
                source_ref="A-prev-1",
                status=AppointmentStatus.NO_SHOW,
                start_at=START - timedelta(days=7),
            ),
            Appointment(
                id="previous-completed",
                clinic_id="clinic-roi",
                patient_id="patient-clinic-roi",
                source_ref="A-prev-2",
                status=AppointmentStatus.COMPLETED,
                start_at=START - timedelta(days=6),
            ),
        ]
    )

    metrics = get_roi_metrics(
        sqlite_session,
        "clinic-roi",
        start=START,
        end=END,
        subscription_cost=Decimal("199.00"),
        usage_cost=Decimal("25.00"),
    )

    assert metrics.contacted == 2
    assert metrics.rebooked == 1
    assert metrics.conversion_rate == 0.5
    assert metrics.recovered_revenue == Decimal("75.00")
    assert metrics.opt_out_rate == 0.5
    assert metrics.no_show_delta == 0.5
    assert metrics.monthly_net == Decimal("-149.00")
    assert metrics.roi_multiple == Decimal("0.38")
    assert "PII body" not in metrics.model_dump_json()


def test_roi_csv_export_contains_aggregate_rows_only(sqlite_session):
    _seed_clinic(sqlite_session, "clinic-csv")
    metrics = get_roi_metrics(sqlite_session, "clinic-csv", start=START, end=END)

    exported = roi_metrics_csv(metrics)

    assert exported.startswith("metric,value")
    assert "contacted" in exported
    assert "patient-clinic-csv" not in exported


def test_roi_metrics_are_scoped_to_the_requested_clinic(sqlite_session):
    _seed_clinic(sqlite_session, "clinic-a")
    _seed_clinic(sqlite_session, "clinic-b")
    sqlite_session.add(
        Interaction(
            id="interaction-clinic-a",
            clinic_id="clinic-a",
            outreach_job_id="job-clinic-a-1",
            channel=Channel.SMS,
            direction=InteractionDirection.OUTBOUND,
            outcome=InteractionOutcome.AUTO_HANDLED,
            occurred_at=START + timedelta(days=2),
        )
    )
    sqlite_session.add(
        BookingAction(
            id="booking-action-clinic-a",
            clinic_id="clinic-a",
            appointment_id="appointment-clinic-a-1",
            outreach_job_id="job-clinic-a-1",
            type=BookingActionType.BOOK,
            status=BookingActionStatus.COMPLETED,
            created_at=START + timedelta(days=5),
            updated_at=START + timedelta(days=5),
        )
    )

    metrics = get_roi_metrics(sqlite_session, "clinic-b", start=START, end=END)

    assert metrics.contacted == 0
    assert metrics.rebooked == 0
    assert metrics.recovered_revenue == Decimal("0.00")