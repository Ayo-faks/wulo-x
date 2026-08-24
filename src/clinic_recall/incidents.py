"""Anonymous incident reporting (LFPSE/Datix-style just-culture reporting).

Deterministic service layer for clinical-governance incident reports filed by
clinic staff (Control Room form) or patients (SMS ``REPORT`` keyword).

Anonymity invariants (SR-aligned, enforced here and by the schema):
- ``IncidentReport`` has no reporter/patient/phone/IP columns at all.
- Patient-sourced reports must never carry ``related_job_id`` (linking a job
  would identify the patient).
- Report timing is coarsened to the hour (``occurred_hour``).
- Audit rows use a fixed generic actor so the audit trail cannot identify the
  reporter either.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .enums import (
    AuditAction,
    IncidentCategory,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from .messaging.audit import audit_action
from .models import IncidentReport, OutreachJob
from .rights import assert_patient_writable

# Fixed audit actor: never the reporter, never a staff username.
_AUDIT_ACTOR = "system:incident-reporting"

# Allowed governance workflow transitions (no skipping back to NEW).
_ALLOWED_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.NEW: frozenset({IncidentStatus.UNDER_REVIEW, IncidentStatus.CLOSED}),
    IncidentStatus.UNDER_REVIEW: frozenset({IncidentStatus.ACTIONED, IncidentStatus.CLOSED}),
    IncidentStatus.ACTIONED: frozenset({IncidentStatus.CLOSED}),
    IncidentStatus.CLOSED: frozenset(),
}

MAX_DESCRIPTION_LENGTH = 4000


def _coarsen_to_hour(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0)


def create_incident(
    session: Session,
    clinic_id: str,
    *,
    source: IncidentSource | str,
    description: str,
    category: IncidentCategory | str = IncidentCategory.OTHER,
    severity: IncidentSeverity | str = IncidentSeverity.NO_HARM,
    related_job_id: str | None = None,
    now: datetime,
) -> IncidentReport:
    """Create one anonymous incident report inside an existing clinic scope."""
    source = IncidentSource(source)
    category = IncidentCategory(category)
    severity = IncidentSeverity(severity)

    text = (description or "").strip()
    if not text:
        raise ValueError("incident description must not be empty")
    if len(text) > MAX_DESCRIPTION_LENGTH:
        text = text[:MAX_DESCRIPTION_LENGTH]

    if source is IncidentSource.PATIENT and related_job_id is not None:
        # Linking a job identifies the patient; fail closed on the invariant.
        raise ValueError("patient-sourced incident reports must not reference a job")
    if related_job_id is not None:
        with clinic_scope(session, clinic_id):
            job = session.execute(
                tenant_select(OutreachJob).where(OutreachJob.id == related_job_id)
            ).scalar_one_or_none()
            if job is None:
                raise LookupError("related outreach job not found for clinic")
            assert_patient_writable(session, clinic_id, job.patient_id)

    report = IncidentReport(
        id=f"incident-{uuid.uuid4().hex}",
        clinic_id=clinic_id,
        source=source,
        category=category,
        severity=severity,
        description=text,
        related_job_id=related_job_id,
        status=IncidentStatus.NEW,
        occurred_hour=_coarsen_to_hour(now),
    )
    session.add(report)
    session.flush()
    audit_action(
        session,
        clinic_id,
        AuditAction.INCIDENT_REPORT,
        report.id,
        # Hash-only payload; category/severity are non-identifying.
        {"source": source.value, "category": category.value, "severity": severity.value},
        actor=_AUDIT_ACTOR,
    )
    return report


def list_incidents(
    session: Session,
    clinic_id: str,
    *,
    status: IncidentStatus | str | None = None,
) -> list[IncidentReport]:
    """List incident reports for one clinic, newest occurrence first."""
    query = sa.select(IncidentReport).where(IncidentReport.clinic_id == clinic_id)
    if status is not None:
        query = query.where(IncidentReport.status == IncidentStatus(status))
    query = query.order_by(IncidentReport.occurred_hour.desc(), IncidentReport.id)
    return list(session.execute(query).scalars())


def update_incident_status(
    session: Session,
    clinic_id: str,
    incident_id: str,
    *,
    status: IncidentStatus | str,
    now: datetime,
) -> IncidentReport:
    """Advance one incident through the governance workflow."""
    new_status = IncidentStatus(status)
    report = session.get(IncidentReport, incident_id)
    if report is None or report.clinic_id != clinic_id:
        raise LookupError(f"incident {incident_id} not found in clinic {clinic_id}")
    if new_status not in _ALLOWED_TRANSITIONS[report.status]:
        raise ValueError(f"invalid incident transition {report.status.value} -> {new_status.value}")
    report.status = new_status
    report.reviewed_at = now
    session.flush()
    audit_action(
        session,
        clinic_id,
        AuditAction.INCIDENT_STATUS_CHANGE,
        report.id,
        {"status": new_status.value},
        actor=_AUDIT_ACTOR,
    )
    return report


# --- Patient-side SMS keyword flow (deterministic; no prompt involvement) ---

REPORT_KEYWORD = "report"

SMS_REPORT_INSTRUCTIONS = (
    "To file an anonymous incident report, text REPORT followed by what "
    "happened. Your name and number are not stored with the report. If this "
    "is urgent or medical, call 999 or contact the clinic directly."
)

SMS_REPORT_CONFIRMATION = (
    "Thank you. Your anonymous incident report has been recorded for the "
    "clinic governance team. Your name and number were not stored with it. "
    "If this is urgent or medical, call 999 or contact the clinic directly."
)


def parse_sms_incident_report(body: str) -> str | None:
    """Return the report description if ``body`` is a REPORT-keyword message.

    Returns ``None`` when the message is not an incident report, and an empty
    string when the caller texted the bare keyword (needs instructions).
    """
    text = (body or "").strip()
    if not text:
        return None
    first, _, rest = text.partition(" ")
    if first.strip().lower().rstrip(".,!:;") != REPORT_KEYWORD:
        return None
    return rest.strip()


def handle_sms_incident_report(
    session: Session,
    clinic_id: str,
    *,
    description: str,
    now: datetime,
) -> str:
    """Record an anonymous patient incident report and return the SMS reply.

    The caller's phone number is deliberately NOT passed into this function so
    it can never be persisted with the report.
    """
    if not description:
        return SMS_REPORT_INSTRUCTIONS
    create_incident(
        session,
        clinic_id,
        source=IncidentSource.PATIENT,
        description=description,
        category=IncidentCategory.OTHER,
        severity=IncidentSeverity.NO_HARM,
        now=now,
    )
    return SMS_REPORT_CONFIRMATION
