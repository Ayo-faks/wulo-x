"""Canonical identity for one deterministic local booking intent."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from .enums import BookingActionType
from .models import AvailabilitySlot


def canonical_booking_request_hash(
    *,
    clinic_id: str,
    patient_id: str,
    appointment_id: str | None,
    slot: AvailabilitySlot,
    action_type: BookingActionType,
    outreach_job_id: str | None,
) -> str:
    """Hash the complete trusted local intent without provider payload data."""
    payload = {
        "action_type": action_type.value,
        "appointment_id": appointment_id or "new",
        "clinic_id": clinic_id,
        "outreach_job_id": outreach_job_id,
        "patient_id": patient_id,
        "schema_version": 1,
        "slot": {
            "appointment_type_id": slot.appointment_type_id,
            "business_id": slot.business_id,
            "end_at": _rfc3339(slot.end_at),
            "practitioner_id": slot.clinician_id,
            "source_provider": slot.source_provider,
            "source_ref": slot.source_ref,
            "start_at": _rfc3339(slot.start_at),
        },
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")