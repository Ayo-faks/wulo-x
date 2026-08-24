"""Fail-closed configuration for durable effect migration slices."""

from __future__ import annotations

import os
from datetime import UTC, datetime

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def durable_sms_enabled() -> bool:
    """Return true only for an explicit recognized migration-switch value."""
    return os.getenv("CLINIC_RECALL_DURABLE_SMS_ENABLED", "false").strip().lower() in _TRUE_VALUES


def operational_snapshot_enabled() -> bool:
    """Return true only when the read-only PR-14 snapshot is explicitly on."""
    return (
        os.getenv("CLINIC_RECALL_OPERATIONAL_SNAPSHOT_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def durable_call_enabled() -> bool:
    """Return true only for an explicit recognized durable CALL value."""
    return (
        os.getenv("CLINIC_RECALL_DURABLE_CALL_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def durable_call_provider_is_twilio() -> bool:
    """Fail closed unless durable CALL dispatch explicitly selects Twilio."""
    return (
        os.getenv("CLINIC_RECALL_DURABLE_CALL_PROVIDER", "").strip().lower()
        == "twilio"
    )


def durable_recording_enabled() -> bool:
    """Return true only for an explicit durable recording dispatch value."""
    return (
        os.getenv("CLINIC_RECALL_DURABLE_RECORDING_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def durable_recording_provider_is_twilio() -> bool:
    """Fail closed unless durable recording dispatch explicitly selects Twilio."""
    return (
        os.getenv("CLINIC_RECALL_DURABLE_RECORDING_PROVIDER", "").strip().lower()
        == "twilio"
    )


def durable_cliniko_write_enabled() -> bool:
    """Return true only for explicit Cliniko booking-write activation."""
    return (
        os.getenv("CLINIC_RECALL_DURABLE_CLINIKO_WRITE_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def cliniko_booking_reconciliation_enabled() -> bool:
    """Return true only for explicit read-only booking reconciliation."""
    return (
        os.getenv(
            "CLINIC_RECALL_CLINIKO_BOOKING_RECONCILIATION_ENABLED",
            "false",
        )
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def durable_booking_confirmation_enabled() -> bool:
    """Return true only for explicit verified-confirmation dispatch."""
    return (
        os.getenv(
            "CLINIC_RECALL_DURABLE_BOOKING_CONFIRMATION_ENABLED",
            "false",
        )
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def durable_rights_enabled() -> bool:
    """Return true only when destructive rights execution is explicitly enabled."""
    return (
        os.getenv("CLINIC_RECALL_DURABLE_RIGHTS_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def durable_rights_twilio_enabled() -> bool:
    """Return true only when the Twilio deletion adapter is explicitly enabled."""
    return (
        os.getenv("CLINIC_RECALL_DURABLE_RIGHTS_TWILIO_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def durable_rights_blob_enabled() -> bool:
    """Return true only when the Blob purge adapter is explicitly enabled."""
    return (
        os.getenv("CLINIC_RECALL_DURABLE_RIGHTS_BLOB_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def retention_scheduling_enabled() -> bool:
    """Return true only when durable retention inventory is explicitly enabled."""
    return (
        os.getenv("CLINIC_RECALL_RETENTION_SCHEDULER_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def callback_application_enabled() -> bool:
    """Return true only when verified callback settlement is explicitly enabled."""
    return (
        os.getenv("CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def handoff_delivery_callback_enabled() -> bool:
    """Return true only after the authenticated Email event route is approved."""
    return (
        os.getenv(
            "CLINIC_RECALL_HANDOFF_DELIVERY_CALLBACK_ENABLED",
            "false",
        )
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def handoff_notification_enabled() -> bool:
    """Return true only after an operational route and notifier are approved."""
    return (
        os.getenv("CLINIC_RECALL_HANDOFF_NOTIFICATION_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def handoff_ageing_enabled() -> bool:
    """Return true only after clinic acknowledgement SLA authority is approved."""
    return (
        os.getenv("CLINIC_RECALL_HANDOFF_AGEING_ENABLED", "false")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def cadence_planning_enabled(now: datetime | None = None) -> bool:
    """Allow planning only with an explicit, complete, fresh configuration."""
    raw_enabled = os.getenv("CLINIC_RECALL_CADENCE_PLANNING_ENABLED")
    refreshed_at = os.getenv("CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT")
    raw_max_age = os.getenv("CLINIC_RECALL_CADENCE_CONFIG_MAX_AGE_SECONDS")
    if raw_enabled is None or raw_enabled.strip().lower() not in _TRUE_VALUES:
        return False
    if not refreshed_at or not raw_max_age:
        return False
    try:
        max_age_seconds = int(raw_max_age)
        refreshed = datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if not 1 <= max_age_seconds <= 3600:
        return False
    if refreshed.tzinfo is None or refreshed.utcoffset() is None:
        return False
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        return False
    age_seconds = (
        current.astimezone(UTC) - refreshed.astimezone(UTC)
    ).total_seconds()
    return 0 <= age_seconds <= max_age_seconds