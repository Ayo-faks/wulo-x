"""Send-time contact counters based on actual outbound interactions."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import InteractionDirection
from ..models import Interaction, OutreachJob
from ..types import ClinicConfig, ContactHistory


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def contact_history_for_send(
    session: Session,
    clinic_id: str,
    patient_id: str,
    now: datetime,
    config: ClinicConfig,
) -> ContactHistory:
    """Return eligibility counters from actual outbound interactions."""
    cutoff_7d = now.astimezone(UTC) - timedelta(days=7)
    local_midnight = now.astimezone(ZoneInfo(config.timezone)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cutoff_today = local_midnight.astimezone(UTC)

    rows = session.execute(
        select(OutreachJob.patient_id, Interaction.occurred_at)
        .join(Interaction, Interaction.outreach_job_id == OutreachJob.id)
        .where(
            OutreachJob.clinic_id == clinic_id,
            Interaction.clinic_id == clinic_id,
            Interaction.direction == InteractionDirection.OUTBOUND,
        )
    ).all()

    patient_counts: Counter[str] = Counter()
    clinic_contacts_today = 0
    for row_patient_id, occurred_at in rows:
        occurred_at_utc = _as_utc(occurred_at)
        if occurred_at_utc >= cutoff_7d:
            patient_counts[row_patient_id] += 1
        if occurred_at_utc >= cutoff_today:
            clinic_contacts_today += 1

    return ContactHistory(
        patient_contacts_last_7d=patient_counts.get(patient_id, 0),
        clinic_contacts_today=clinic_contacts_today,
    )