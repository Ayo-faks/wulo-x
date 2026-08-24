"""Deterministic candidate detection (FR-05).

Pure functions that classify an appointment into exactly one outreach
``ReasonCode`` (or ``None`` when no outreach is due). There is no model
judgement here: the reason is a deterministic function of appointment status
and timestamp arithmetic against per-clinic windows, so it is fully testable
against a truth table and auditable.

Precedence (first match wins), by appointment status:

1. ``cancelled``              -> ``CANCELLED``
2. ``missed`` / ``no_show``   -> ``MISSED``
3. ``completed``              -> ``OVERDUE_FOLLOWUP`` when the last completed
   visit is older than the clinic's follow-up interval and the patient has no
   future appointment; otherwise ``None``.
4. ``scheduled``:
   - start time already in the past         -> ``MISSED``
   - within the upcoming-reminder window     -> ``UPCOMING_REMINDER``
   - within the due-recurring pre-window      -> ``DUE_RECURRING``
   - otherwise                                -> ``None``
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .enums import AppointmentStatus, ReasonCode
from .types import AppointmentView, ClinicConfig


def _require_aware(value: datetime, name: str) -> None:
    """Reject naive datetimes so detection never compares ambiguous times."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (got naive datetime)")


def classify_reason(
    appointment: AppointmentView,
    config: ClinicConfig,
    now: datetime,
) -> ReasonCode | None:
    """Classify a single appointment into an outreach reason code.

    Args:
        appointment: The appointment facts to classify.
        config: The owning clinic's detection windows.
        now: Timezone-aware "current" instant (UTC) to evaluate against.

    Returns:
        The matched :class:`ReasonCode`, or ``None`` if no outreach is due.

    Raises:
        ValueError: If ``now`` or any compared timestamp is naive.
    """
    _require_aware(now, "now")
    status = appointment.status

    if status == AppointmentStatus.CANCELLED:
        return ReasonCode.CANCELLED

    if status in (AppointmentStatus.MISSED, AppointmentStatus.NO_SHOW):
        return ReasonCode.MISSED

    if status == AppointmentStatus.COMPLETED:
        return _classify_completed(appointment, config, now)

    if status == AppointmentStatus.SCHEDULED:
        return _classify_scheduled(appointment, config, now)

    return None


def _classify_completed(
    appointment: AppointmentView,
    config: ClinicConfig,
    now: datetime,
) -> ReasonCode | None:
    """Overdue-follow-up detection for a completed visit."""
    if appointment.has_future_appointment:
        return None
    reference = appointment.last_completed_at or appointment.start_at
    _require_aware(reference, "last_completed_at/start_at")
    if reference + config.overdue_followup_interval < now:
        return ReasonCode.OVERDUE_FOLLOWUP
    return None


def _classify_scheduled(
    appointment: AppointmentView,
    config: ClinicConfig,
    now: datetime,
) -> ReasonCode | None:
    """Reminder / due-recurring / lapsed detection for a scheduled visit."""
    _require_aware(appointment.start_at, "start_at")
    delta = appointment.start_at - now

    # Scheduled but the start time already passed without completion: lapsed.
    if delta <= timedelta(0):
        return ReasonCode.MISSED

    if delta <= timedelta(hours=config.upcoming_reminder_hours):
        return ReasonCode.UPCOMING_REMINDER

    if (
        timedelta(days=config.due_recurring_min_days)
        <= delta
        <= timedelta(days=config.due_recurring_max_days)
    ):
        return ReasonCode.DUE_RECURRING

    return None
