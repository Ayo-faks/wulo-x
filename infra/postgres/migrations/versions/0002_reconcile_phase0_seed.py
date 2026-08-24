"""reconcile phase 0 synthetic seed into clinic/patient/appointment

Revision ID: 0002_reconcile_phase0_seed
Revises: 0001_initial_schema
Create Date: 2026-06-26

Supersedes the Phase 0 spike table ``phase0_missed_appointments`` (a single
denormalised table) by migrating its rows into the real ``clinic`` /
``patient`` / ``appointment`` tables, then dropping it so there are not two
parallel schemas. Safe to run on a database that never had the spike table
(fresh installs simply skip the data step).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session
from src.clinic_recall.enums import AppointmentStatus
from src.clinic_recall.models import Appointment, Clinic, Patient

revision: str = "0002_reconcile_phase0_seed"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PHASE0_TABLE = "phase0_missed_appointments"

# The Phase 0 table conflated lifecycle status with the outreach reason. Map its
# values onto real appointment lifecycle statuses; detection later derives the
# reason_code deterministically from these.
_STATUS_MAP = {
    "missed": AppointmentStatus.MISSED,
    "cancelled": AppointmentStatus.CANCELLED,
    "overdue_followup": AppointmentStatus.COMPLETED,
}


def upgrade() -> None:
    """Migrate the synthetic seed, then drop the spike table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _PHASE0_TABLE not in inspector.get_table_names():
        return  # Fresh database: nothing to reconcile.

    rows = list(
        bind.execute(
            sa.text(
                f"SELECT appointment_id, clinic_id, patient_ref, patient_display_name, "
                f"patient_phone, appointment_start, status, consent_to_contact "
                f"FROM {_PHASE0_TABLE}"
            )
        ).mappings()
    )

    session = Session(bind=bind)
    clinic_ids = {row["clinic_id"] for row in rows}
    for clinic_id in clinic_ids:
        # The clinic table has no RLS; tenant tables below need app.clinic_id set.
        if bind.dialect.name == "postgresql":
            session.execute(
                sa.text("SELECT set_config('app.clinic_id', :cid, false)"),
                {"cid": clinic_id},
            )
        session.merge(
            Clinic(
                id=clinic_id,
                name="Phase 0 Synthetic Clinic",
                timezone="Europe/London",
                daily_caps=200,
            )
        )
        session.flush()

        for row in (r for r in rows if r["clinic_id"] == clinic_id):
            consent = bool(row["consent_to_contact"])
            session.merge(
                Patient(
                    id=row["patient_ref"],
                    clinic_id=clinic_id,
                    source_ref=row["patient_ref"],
                    name=row["patient_display_name"],
                    phone=row["patient_phone"],
                    consent_flags={"sms": consent, "call": consent, "email": False},
                    opt_out_flags={},
                )
            )
            session.merge(
                Appointment(
                    id=row["appointment_id"],
                    clinic_id=clinic_id,
                    patient_id=row["patient_ref"],
                    source_ref=row["appointment_id"],
                    status=_STATUS_MAP.get(row["status"], AppointmentStatus.MISSED),
                    start_at=row["appointment_start"],
                )
            )
        session.flush()

    op.drop_table(_PHASE0_TABLE)


def downgrade() -> None:
    """Irreversible data migration: the spike table cannot be reconstructed."""
    # Intentionally a no-op. Re-seed via infra/postgres/phase0_missed_appointments.sql
    # (kept as an archived reference) if the spike table is needed again.
