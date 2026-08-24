"""Per-channel opt-out persistence for Clinic Recall messaging."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..enums import AuditAction, Channel
from ..models import Patient
from ..rights import SubjectFrozenError, assert_patient_writable
from ..telemetry import queue_after_commit
from .audit import audit_action


def record_opt_out(
    session: Session,
    clinic_id: str,
    patient: Patient,
    channel: Channel,
    now: datetime,
) -> None:
    """Immediately and permanently opt a patient out of one channel."""
    try:
        assert_patient_writable(session, clinic_id, patient.id)
    except SubjectFrozenError:
        return
    flags = dict(patient.opt_out_flags or {})
    flags[channel.value] = True
    patient.opt_out_flags = flags
    audit_action(
        session,
        clinic_id,
        AuditAction.OPT_OUT_PATIENT,
        patient.id,
        {"channel": channel.value, "patient_id": patient.id, "occurred_at": now},
    )
    queue_after_commit(
        session,
        "voice.optout.recorded",
        {"channel": channel.value},
    )