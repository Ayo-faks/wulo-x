"""Deterministic post-visit feedback queue generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .candidate_queue import clinic_config_from_row
from .db import clinic_scope, tenant_select
from .eligibility import evaluate
from .enums import (
    AppointmentStatus,
    AuditAction,
    CampaignStatus,
    CampaignType,
    Channel,
    OutreachState,
)
from .messaging.audit import audit_action
from .models import Appointment, Campaign, Clinic, OutreachJob, Patient
from .pilot_controls import PatientPilotGate
from .rights import SubjectFrozenError, assert_patient_writable
from .sync.base import make_id
from .types import ContactHistory


@dataclass(frozen=True)
class FeedbackQueueResult:
    """Counts from one feedback queue generation run."""

    detected: int = 0
    queued: int = 0
    already_queued: int = 0
    skipped: dict[str, int] | None = None

    def as_summary(self) -> dict[str, object]:
        return {
            "detected": self.detected,
            "queued": self.queued,
            "already_queued": self.already_queued,
            "skipped": dict(self.skipped or {}),
        }


def generate_feedback_queue(
    session: Session,
    clinic_id: str,
    now: datetime,
    *,
    channel: Channel = Channel.SMS,
    pilot_gate: PatientPilotGate,
) -> FeedbackQueueResult:
    """Queue feedback requests for completed visits in one clinic.

    Nothing is sent here. Send-time eligibility is still re-checked by
    ``messaging.send`` before any SMS/email leaves the system.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (got naive datetime)")

    detected = 0
    queued = 0
    already_queued = 0
    skipped: dict[str, int] = {}
    with clinic_scope(session, clinic_id):
        clinic = session.get(Clinic, clinic_id)
        if clinic is None:
            raise LookupError(f"clinic {clinic_id!r} not found")
        campaign = _ensure_feedback_campaign(session, clinic_id)
        cfg = clinic_config_from_row(clinic)

        appointments = list(
            session.execute(
                tenant_select(Appointment)
                .where(Appointment.status == AppointmentStatus.COMPLETED)
                .order_by(Appointment.start_at, Appointment.id)
            ).scalars()
        )
        for appointment in appointments:
            if _as_utc(appointment.start_at) > now:
                continue
            detected += 1
            patient = session.get(Patient, appointment.patient_id)
            if patient is None:  # pragma: no cover - FK guarantees presence
                continue
            try:
                assert_patient_writable(session, clinic_id, patient.id)
            except SubjectFrozenError:
                skipped["subject_frozen"] = skipped.get("subject_frozen", 0) + 1
                audit_action(
                    session,
                    clinic_id,
                    AuditAction.SKIP_CANDIDATE,
                    appointment.id,
                    {
                        "campaign_type": CampaignType.FEEDBACK.value,
                        "skip_reason": "subject_frozen",
                    },
                    actor="system:feedback",
                )
                continue
            pilot_decision = pilot_gate(
                session,
                clinic_id,
                patient.id,
                channel,
                now,
            )
            if not pilot_decision.allowed:
                skipped[pilot_decision.reason] = (
                    skipped.get(pilot_decision.reason, 0) + 1
                )
                continue
            decision = evaluate(
                _patient_view(patient),
                cfg,
                ContactHistory(patient_contacts_last_7d=0, clinic_contacts_today=0),
                now,
                channel,
            )
            if not decision.eligible:
                assert decision.skip_reason is not None  # nosec B101
                skipped[decision.skip_reason.value] = skipped.get(decision.skip_reason.value, 0) + 1
                continue
            existing = session.execute(
                select(OutreachJob).where(
                    OutreachJob.clinic_id == clinic_id,
                    OutreachJob.campaign_id == campaign.id,
                    OutreachJob.appointment_id == appointment.id,
                    OutreachJob.channel == channel,
                )
            ).scalar_one_or_none()
            if existing is not None:
                already_queued += 1
                continue
            job_id = make_id("job", clinic_id, f"feedback:{appointment.id}:{channel.value}")
            session.add(
                OutreachJob(
                    id=job_id,
                    clinic_id=clinic_id,
                    campaign_id=campaign.id,
                    patient_id=patient.id,
                    appointment_id=appointment.id,
                    channel=channel,
                    state=OutreachState.QUEUED,
                )
            )
            queued += 1
            audit_action(
                session,
                clinic_id,
                AuditAction.ENQUEUE_OUTREACH,
                job_id,
                {
                    "appointment_id": appointment.id,
                    "channel": channel.value,
                    "campaign_type": CampaignType.FEEDBACK.value,
                    "occurred_at": now,
                },
                actor="system:feedback",
            )
        session.flush()
    return FeedbackQueueResult(detected=detected, queued=queued, already_queued=already_queued, skipped=skipped)


def _ensure_feedback_campaign(session: Session, clinic_id: str) -> Campaign:
    campaign_id = f"campaign-feedback-{clinic_id}"
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        campaign = Campaign(
            id=campaign_id,
            clinic_id=clinic_id,
            type=CampaignType.FEEDBACK,
            status=CampaignStatus.ACTIVE,
        )
        session.add(campaign)
        session.flush()
    return campaign


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _patient_view(patient: Patient):
    from .candidate_queue import _patient_view as to_patient_view

    return to_patient_view(patient)