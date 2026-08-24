"""Truth-table tests for deterministic candidate detection (FR-05)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from src.clinic_recall.detection import classify_reason
from src.clinic_recall.enums import AppointmentStatus, ReasonCode
from src.clinic_recall.types import AppointmentView, ClinicConfig

NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
CFG = ClinicConfig(clinic_id="clinic-test")


def _appt(
    status: AppointmentStatus,
    *,
    start_at: datetime = NOW,
    last_completed_at: datetime | None = None,
    has_future_appointment: bool = False,
) -> AppointmentView:
    return AppointmentView(
        appointment_id="appt-1",
        clinic_id="clinic-test",
        patient_id="patient-1",
        status=status,
        start_at=start_at,
        last_completed_at=last_completed_at,
        has_future_appointment=has_future_appointment,
    )


@pytest.mark.parametrize(
    ("appointment", "expected"),
    [
        # Status-driven reasons.
        (_appt(AppointmentStatus.CANCELLED), ReasonCode.CANCELLED),
        (_appt(AppointmentStatus.MISSED), ReasonCode.MISSED),
        (_appt(AppointmentStatus.NO_SHOW), ReasonCode.MISSED),
        # Scheduled but the start time already passed -> lapsed/missed.
        (_appt(AppointmentStatus.SCHEDULED, start_at=NOW - timedelta(hours=1)), ReasonCode.MISSED),
        # Upcoming-reminder window (<= 48h), including the exact boundary.
        (
            _appt(AppointmentStatus.SCHEDULED, start_at=NOW + timedelta(hours=24)),
            ReasonCode.UPCOMING_REMINDER,
        ),
        (
            _appt(AppointmentStatus.SCHEDULED, start_at=NOW + timedelta(hours=48)),
            ReasonCode.UPCOMING_REMINDER,
        ),
        # Gap between reminder window and due-recurring pre-window -> no outreach.
        (_appt(AppointmentStatus.SCHEDULED, start_at=NOW + timedelta(hours=49)), None),
        (_appt(AppointmentStatus.SCHEDULED, start_at=NOW + timedelta(days=6)), None),
        # Due-recurring pre-window [7, 14] days, including both boundaries.
        (
            _appt(AppointmentStatus.SCHEDULED, start_at=NOW + timedelta(days=7)),
            ReasonCode.DUE_RECURRING,
        ),
        (
            _appt(AppointmentStatus.SCHEDULED, start_at=NOW + timedelta(days=10)),
            ReasonCode.DUE_RECURRING,
        ),
        (
            _appt(AppointmentStatus.SCHEDULED, start_at=NOW + timedelta(days=14)),
            ReasonCode.DUE_RECURRING,
        ),
        # Beyond the due-recurring window -> no outreach yet.
        (_appt(AppointmentStatus.SCHEDULED, start_at=NOW + timedelta(days=15)), None),
        # Overdue follow-up: last completed visit older than 28 days, no future appt.
        (
            _appt(
                AppointmentStatus.COMPLETED,
                last_completed_at=NOW - timedelta(days=29),
            ),
            ReasonCode.OVERDUE_FOLLOWUP,
        ),
        # Exactly at the 28-day interval is NOT yet overdue.
        (
            _appt(
                AppointmentStatus.COMPLETED,
                last_completed_at=NOW - timedelta(days=28),
            ),
            None,
        ),
        # Not yet overdue.
        (
            _appt(
                AppointmentStatus.COMPLETED,
                last_completed_at=NOW - timedelta(days=27),
            ),
            None,
        ),
        # Overdue interval elapsed but a future appointment suppresses outreach.
        (
            _appt(
                AppointmentStatus.COMPLETED,
                last_completed_at=NOW - timedelta(days=40),
                has_future_appointment=True,
            ),
            None,
        ),
        # No last_completed_at -> fall back to start_at for the overdue test.
        (
            _appt(AppointmentStatus.COMPLETED, start_at=NOW - timedelta(days=29)),
            ReasonCode.OVERDUE_FOLLOWUP,
        ),
    ],
)
def test_classify_reason_truth_table(appointment, expected):
    assert classify_reason(appointment, CFG, NOW) == expected


def test_classify_reason_rejects_naive_now():
    naive = datetime(2026, 6, 26, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        classify_reason(_appt(AppointmentStatus.MISSED), CFG, naive)


def test_per_clinic_windows_are_configurable():
    # A clinic with a 7-day overdue interval flags a 10-day-old completion.
    aggressive = ClinicConfig(clinic_id="clinic-aggressive", overdue_followup_days=7)
    appt = _appt(AppointmentStatus.COMPLETED, last_completed_at=NOW - timedelta(days=10))
    assert classify_reason(appt, aggressive, NOW) == ReasonCode.OVERDUE_FOLLOWUP
    # The default 28-day clinic does not.
    assert classify_reason(appt, CFG, NOW) is None
