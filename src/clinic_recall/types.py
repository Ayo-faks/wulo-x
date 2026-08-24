"""Framework-free value objects for deterministic detection and eligibility.

Detection (FR-05) and eligibility (FR-06) are pure functions over these plain
dataclasses, deliberately decoupled from SQLAlchemy ORM rows so they can be
unit-tested exhaustively without a database. The persistence layer adapts ORM
rows into these views.

All datetimes are timezone-aware and compared in UTC. Clinic-local time is only
used for the contact-hours / quiet-hours check, via ``ClinicConfig.timezone``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .enums import AppointmentStatus

# ---------------------------------------------------------------------------
# MVP default thresholds.
#
# The PRD (FR-05/FR-06/FR-09) mandates these gates but deliberately leaves the
# exact numbers to per-clinic configuration. These are the documented MVP
# defaults; every value is overridable per clinic via ``ClinicConfig``.
# ---------------------------------------------------------------------------
DEFAULT_OVERDUE_FOLLOWUP_DAYS = 28
DEFAULT_DUE_RECURRING_MIN_DAYS = 7
DEFAULT_DUE_RECURRING_MAX_DAYS = 14
DEFAULT_UPCOMING_REMINDER_HOURS = 48
DEFAULT_PER_PATIENT_WEEKLY_CAP = 3
DEFAULT_DAILY_CLINIC_CAP = 200
DEFAULT_CONTACT_START_HOUR = 8  # 08:00 clinic-local, inclusive
DEFAULT_CONTACT_END_HOUR = 20  # 20:00 clinic-local, exclusive
DEFAULT_TIMEZONE = "Europe/London"


@dataclass(frozen=True)
class ClinicConfig:
    """Per-clinic configuration for detection windows and eligibility gates.

    Defaults reflect the MVP policy; production clinics override these via the
    ``clinic`` table (``contact_hours``, ``daily_caps``, ``consent_policy``).
    """

    clinic_id: str
    timezone: str = DEFAULT_TIMEZONE
    contact_start_hour: int = DEFAULT_CONTACT_START_HOUR
    contact_end_hour: int = DEFAULT_CONTACT_END_HOUR
    daily_cap: int = DEFAULT_DAILY_CLINIC_CAP
    per_patient_weekly_cap: int = DEFAULT_PER_PATIENT_WEEKLY_CAP
    overdue_followup_days: int = DEFAULT_OVERDUE_FOLLOWUP_DAYS
    due_recurring_min_days: int = DEFAULT_DUE_RECURRING_MIN_DAYS
    due_recurring_max_days: int = DEFAULT_DUE_RECURRING_MAX_DAYS
    upcoming_reminder_hours: int = DEFAULT_UPCOMING_REMINDER_HOURS

    @property
    def overdue_followup_interval(self) -> timedelta:
        """The follow-up interval after which a completed visit is overdue."""
        return timedelta(days=self.overdue_followup_days)

    @property
    def contact_window(self) -> tuple[time, time]:
        """The clinic-local allowed contact window as ``(start, end)`` times."""
        return time(hour=self.contact_start_hour), time(hour=self.contact_end_hour)


@dataclass(frozen=True)
class AppointmentView:
    """The minimal appointment facts detection needs (decoupled from ORM).

    Attributes:
        appointment_id: Stable internal appointment id.
        clinic_id: Owning clinic (tenant) id.
        patient_id: Owning patient id.
        status: Normalised appointment status.
        start_at: Timezone-aware appointment start (UTC).
        last_completed_at: When the patient last completed an appointment, used
            for ``overdue_followup`` detection. ``None`` if never completed.
        has_future_appointment: Whether the patient already has a future
            scheduled appointment (suppresses ``overdue_followup``).
    """

    appointment_id: str
    clinic_id: str
    patient_id: str
    status: AppointmentStatus
    start_at: datetime
    last_completed_at: datetime | None = None
    has_future_appointment: bool = False


@dataclass(frozen=True)
class PatientView:
    """The minimal patient facts eligibility needs (decoupled from ORM).

    Attributes:
        patient_id: Patient id.
        clinic_id: Owning clinic (tenant) id.
        phone: E.164 phone or ``None``.
        email: Email address or ``None``.
        consent_flags: Per-channel consent, e.g. ``{"sms": True}``. Missing or
            falsey means no consent (fail closed).
        opt_out_flags: Per-channel permanent opt-outs, e.g. ``{"sms": True}``.
        quiet_hours: Optional patient-specific quiet window ``(start, end)`` in
            clinic-local time; outreach is suppressed inside it.
    """

    patient_id: str
    clinic_id: str
    phone: str | None = None
    email: str | None = None
    consent_flags: dict[str, bool] = field(default_factory=dict)
    opt_out_flags: dict[str, bool] = field(default_factory=dict)
    quiet_hours: tuple[time, time] | None = None


@dataclass(frozen=True)
class ContactHistory:
    """Rolling contact counters used by frequency / daily-cap gates.

    Attributes:
        patient_contacts_last_7d: Outreach attempts to this patient in the
            trailing 7 days (across all channels).
        clinic_contacts_today: Outreach attempts the clinic has made on the
            current clinic-local day (across all patients/channels).
    """

    patient_contacts_last_7d: int = 0
    clinic_contacts_today: int = 0
