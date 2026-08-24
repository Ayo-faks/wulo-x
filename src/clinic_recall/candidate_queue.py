"""Candidate queue generation (FR-05 detection + FR-06 eligibility -> queue).

The deterministic heart of Phase 1. For one clinic it:

1. ensures a standing "detection" campaign exists (so queued jobs satisfy the
   ``outreach_job -> campaign`` foreign key without doing any Phase 2 sending);
2. classifies every appointment into a reason code (:mod:`detection`);
3. applies the eligibility gates (:mod:`eligibility`) with per-run frequency and
   daily caps enforced cumulatively;
4. writes a ``queued`` ``outreach_job`` for each eligible candidate (idempotently
   — re-running never duplicates a job) and records the detection reason on the
   appointment;
5. audits every outcome (``enqueue_outreach`` / ``skip_candidate``).

Nothing is sent here. Producing the queue is the Phase 1 deliverable.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .detection import classify_reason
from .eligibility import evaluate
from .enums import AppointmentStatus, AuditAction, CampaignStatus, CampaignType, Channel
from .models import Appointment, AuditLog, Campaign, Clinic, OutreachJob, Patient
from .pilot_controls import PatientPilotGate
from .sync.base import make_id
from .types import (
    DEFAULT_CONTACT_END_HOUR,
    DEFAULT_CONTACT_START_HOUR,
    DEFAULT_DAILY_CLINIC_CAP,
    DEFAULT_TIMEZONE,
    AppointmentView,
    ClinicConfig,
    ContactHistory,
    PatientView,
)

_ACTOR = "system:candidate_detection"


@dataclass
class CandidateQueueResult:
    """Counts from one candidate-queue run."""

    detected: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    queued: int = 0
    already_queued: int = 0

    @property
    def detected_total(self) -> int:
        """Total candidates detected (across all reason codes)."""
        return sum(self.detected.values())

    def as_summary(self) -> dict[str, object]:
        """A JSON-friendly summary for logging / demos."""
        return {
            "detected": dict(self.detected),
            "detected_total": self.detected_total,
            "queued": self.queued,
            "already_queued": self.already_queued,
            "skipped": dict(self.skipped),
        }


def _as_utc(value: datetime) -> datetime:
    """Treat a DB timestamp as UTC (SQLite drops tzinfo; PostgreSQL keeps it)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def clinic_config_from_row(clinic: Clinic) -> ClinicConfig:
    """Build a :class:`ClinicConfig` from a clinic row, falling back to defaults."""
    contact_hours = clinic.contact_hours or {}
    return ClinicConfig(
        clinic_id=clinic.id,
        timezone=clinic.timezone or DEFAULT_TIMEZONE,
        contact_start_hour=int(contact_hours.get("start_hour", DEFAULT_CONTACT_START_HOUR)),
        contact_end_hour=int(contact_hours.get("end_hour", DEFAULT_CONTACT_END_HOUR)),
        daily_cap=clinic.daily_caps if clinic.daily_caps is not None else DEFAULT_DAILY_CLINIC_CAP,
    )


def _quiet_hours(contact_prefs: dict | None) -> tuple[time, time] | None:
    """Parse an optional patient quiet-hours window from contact_prefs."""
    if not contact_prefs:
        return None
    start = contact_prefs.get("quiet_start_hour")
    end = contact_prefs.get("quiet_end_hour")
    if start is None or end is None:
        return None
    return time(hour=int(start)), time(hour=int(end))


def _patient_view(patient: Patient) -> PatientView:
    return PatientView(
        patient_id=patient.id,
        clinic_id=patient.clinic_id,
        phone=patient.phone,
        email=patient.email,
        consent_flags=dict(patient.consent_flags or {}),
        opt_out_flags=dict(patient.opt_out_flags or {}),
        quiet_hours=_quiet_hours(patient.contact_prefs),
    )


def _ensure_detection_campaign(session: Session, clinic_id: str) -> Campaign:
    """Find or create the clinic's standing detection campaign."""
    campaign_id = f"campaign-detection-{clinic_id}"
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        campaign = Campaign(
            id=campaign_id,
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.DRAFT,
        )
        session.add(campaign)
        session.flush()
    elif campaign.status != CampaignStatus.DRAFT:
        campaign.status = CampaignStatus.DRAFT
        session.flush()
    return campaign


def _audit(
    session: Session,
    clinic_id: str,
    action: AuditAction,
    entity_ref: str,
    payload: dict[str, object],
) -> None:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    session.add(
        AuditLog(
            id=f"audit-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            actor=_ACTOR,
            action=action,
            entity_ref=entity_ref,
            payload_hash=hashlib.sha256(encoded).hexdigest(),
        )
    )


def generate_candidate_queue(
    session: Session,
    clinic_id: str,
    now: datetime,
    channel: Channel = Channel.SMS,
    config: ClinicConfig | None = None,
    *,
    pilot_gate: PatientPilotGate,
) -> CandidateQueueResult:
    """Detect, filter, and enqueue outreach candidates for one clinic.

    Args:
        session: An open SQLAlchemy session (the caller commits).
        clinic_id: The clinic to process; all reads/writes are scoped to it.
        now: Timezone-aware "current" instant (UTC).
        channel: The intended first-contact channel (defaults to SMS).
        config: Optional explicit clinic config (otherwise built from the row).

    Returns:
        A :class:`CandidateQueueResult` with detection / queue / skip counts.

    Raises:
        LookupError: If the clinic does not exist.
        ValueError: If ``now`` is naive.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (got naive datetime)")

    result = CandidateQueueResult()
    with clinic_scope(session, clinic_id):
        clinic = session.get(Clinic, clinic_id)
        if clinic is None:
            raise LookupError(f"clinic {clinic_id!r} not found")
        cfg = config or clinic_config_from_row(clinic)
        campaign = _ensure_detection_campaign(session, clinic_id)

        appointments = list(session.execute(tenant_select(Appointment)).scalars().all())

        # Per-patient aggregates computed once from the scoped appointment set.
        last_completed: dict[str, datetime] = {}
        future_patients: set[str] = set()
        for appt in appointments:
            start_at = _as_utc(appt.start_at)
            if appt.status == AppointmentStatus.COMPLETED:
                prior = last_completed.get(appt.patient_id)
                if prior is None or start_at > prior:
                    last_completed[appt.patient_id] = start_at
            if appt.status == AppointmentStatus.SCHEDULED and start_at > now:
                future_patients.add(appt.patient_id)

        # Running cap counters (seeded from existing jobs, incremented as we queue).
        patient_counts = _patient_job_counts(session, clinic_id, now)
        clinic_count = _clinic_jobs_today(session, clinic_id, now, cfg)

        for appt in appointments:
            view = AppointmentView(
                appointment_id=appt.id,
                clinic_id=appt.clinic_id,
                patient_id=appt.patient_id,
                status=appt.status,
                start_at=_as_utc(appt.start_at),
                last_completed_at=last_completed.get(appt.patient_id),
                has_future_appointment=appt.patient_id in future_patients,
            )
            reason = classify_reason(view, cfg, now)
            if reason is None:
                continue
            result.detected[reason.value] += 1

            patient = session.get(Patient, appt.patient_id)
            if patient is None:  # pragma: no cover - FK guarantees presence
                continue
            from .rights import SubjectFrozenError, assert_patient_writable

            try:
                assert_patient_writable(session, clinic_id, patient.id)
            except SubjectFrozenError:
                result.skipped["subject_frozen"] += 1
                _audit(
                    session,
                    clinic_id,
                    AuditAction.SKIP_CANDIDATE,
                    appt.id,
                    {"reason": reason.value, "skip_reason": "subject_frozen"},
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
                result.skipped[pilot_decision.reason] += 1
                _audit(
                    session,
                    clinic_id,
                    AuditAction.SKIP_CANDIDATE,
                    appt.id,
                    {
                        "reason": reason.value,
                        "skip_reason": pilot_decision.reason,
                    },
                )
                continue
            history = ContactHistory(
                patient_contacts_last_7d=patient_counts.get(appt.patient_id, 0),
                clinic_contacts_today=clinic_count,
            )
            decision = evaluate(_patient_view(patient), cfg, history, now, channel)
            if not decision.eligible:
                assert decision.skip_reason is not None  # nosec B101
                result.skipped[decision.skip_reason.value] += 1
                _audit(
                    session,
                    clinic_id,
                    AuditAction.SKIP_CANDIDATE,
                    appt.id,
                    {"reason": reason.value, "skip_reason": decision.skip_reason.value},
                )
                continue

            # Record the detection reason on the appointment.
            appt.reason_code = reason

            existing = session.execute(
                select(OutreachJob).where(
                    OutreachJob.clinic_id == clinic_id,
                    OutreachJob.appointment_id == appt.id,
                    OutreachJob.channel == channel,
                )
            ).scalar_one_or_none()
            if existing is not None:
                result.already_queued += 1
                continue

            job_id = make_id("job", clinic_id, f"{appt.id}:{channel.value}")
            session.add(
                OutreachJob(
                    id=job_id,
                    clinic_id=clinic_id,
                    campaign_id=campaign.id,
                    patient_id=appt.patient_id,
                    appointment_id=appt.id,
                    channel=channel,
                    reason_code=reason,
                )
            )
            result.queued += 1
            patient_counts[appt.patient_id] = patient_counts.get(appt.patient_id, 0) + 1
            clinic_count += 1
            _audit(
                session,
                clinic_id,
                AuditAction.ENQUEUE_OUTREACH,
                job_id,
                {"appointment_id": appt.id, "channel": channel.value, "reason": reason.value},
            )

        session.flush()
    return result


def _patient_job_counts(session: Session, clinic_id: str, now: datetime) -> dict[str, int]:
    """Outreach-job counts per patient within the trailing 7 days.

    Windowing is done in Python so the same logic is correct on PostgreSQL
    (tz-aware ``created_at``) and SQLite (tz-naive ``created_at``).
    """
    cutoff = now.astimezone(UTC) - timedelta(days=7)
    rows = session.execute(
        select(OutreachJob.patient_id, OutreachJob.created_at).where(
            OutreachJob.clinic_id == clinic_id
        )
    ).all()
    counts: Counter[str] = Counter()
    for patient_id, created_at in rows:
        if _as_utc(created_at) >= cutoff:
            counts[patient_id] += 1
    return dict(counts)


def _clinic_jobs_today(session: Session, clinic_id: str, now: datetime, cfg: ClinicConfig) -> int:
    """Outreach-job count for the clinic so far on the local calendar day."""
    local_midnight = now.astimezone(ZoneInfo(cfg.timezone)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cutoff = local_midnight.astimezone(UTC)
    created = (
        session.execute(select(OutreachJob.created_at).where(OutreachJob.clinic_id == clinic_id))
        .scalars()
        .all()
    )
    return sum(1 for created_at in created if _as_utc(created_at) >= cutoff)
