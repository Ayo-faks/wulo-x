"""Confirmation helpers and fail-closed legacy erasure compatibility."""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn

from sqlalchemy.orm import Session

_LEGACY_ERASURE_DISABLED = (
    "synchronous patient erasure is disabled; use request_patient_erasure and "
    "the durable rights worker"
)


def erasure_confirm_token(patient_id: str) -> str:
    """Return the explicit confirmation phrase required for patient erasure."""
    return f"ERASE {patient_id}"


def erase_patient(
    session: Session,
    clinic_id: str,
    patient_id: str,
    *,
    confirm_token: str,
    now: datetime,
    actor: str,
) -> NoReturn:
    """Reject the removed inline-delete path before it can mutate any data."""
    del session, clinic_id, patient_id, confirm_token, now, actor
    raise RuntimeError(_LEGACY_ERASURE_DISABLED)