"""add phase 3 availability slots and booking idempotency keys

Revision ID: 0004_phase3_availability_slots
Revises: 0003_add_clinic_sms_number
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from src.clinic_recall.models import RLS_GUC

revision: str = "0004_phase3_availability_slots"
down_revision: str | None = "0003_add_clinic_sms_number"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add real-slot availability and booking idempotency columns."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "availability_slot" not in tables:
        op.create_table(
            "availability_slot",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("clinic_id", sa.String(), nullable=False),
            sa.Column("source_ref", sa.String(), nullable=False),
            sa.Column("clinician_id", sa.String(), nullable=True),
            sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("appointment_id", sa.String(), nullable=True),
            sa.Column("details", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["appointment_id"], ["appointment.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("clinic_id", "source_ref", name="uq_availability_slot_source"),
        )

    indexes = {
        index["name"]
        for index in inspector.get_indexes("availability_slot")
    } if "availability_slot" in set(sa.inspect(bind).get_table_names()) else set()
    if "ix_availability_slot_clinic_clinician" not in indexes:
        op.create_index(
            "ix_availability_slot_clinic_clinician",
            "availability_slot",
            ["clinic_id", "clinician_id"],
        )
    if "ix_availability_slot_clinic_id" not in indexes:
        op.create_index("ix_availability_slot_clinic_id", "availability_slot", ["clinic_id"])
    if "ix_availability_slot_clinic_start" not in indexes:
        op.create_index(
            "ix_availability_slot_clinic_start", "availability_slot", ["clinic_id", "start_at"]
        )
    if "ix_availability_slot_clinician_id" not in indexes:
        op.create_index("ix_availability_slot_clinician_id", "availability_slot", ["clinician_id"])
    if "ix_availability_slot_appointment_id" not in indexes:
        op.create_index("ix_availability_slot_appointment_id", "availability_slot", ["appointment_id"])

    booking_columns = {column["name"] for column in inspector.get_columns("booking_action")}
    if "outreach_job_id" not in booking_columns:
        op.add_column("booking_action", sa.Column("outreach_job_id", sa.String(), nullable=True))
    if "availability_slot_id" not in booking_columns:
        op.add_column("booking_action", sa.Column("availability_slot_id", sa.String(), nullable=True))

    foreign_keys = {constraint["name"] for constraint in inspector.get_foreign_keys("booking_action")}
    if bind.dialect.name != "sqlite" and "fk_booking_action_outreach_job_id_outreach_job" not in foreign_keys:
        op.create_foreign_key(
            "fk_booking_action_outreach_job_id_outreach_job",
            "booking_action",
            "outreach_job",
            ["outreach_job_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if (
        bind.dialect.name != "sqlite"
        and "fk_booking_action_availability_slot_id_availability_slot" not in foreign_keys
    ):
        op.create_foreign_key(
            "fk_booking_action_availability_slot_id_availability_slot",
            "booking_action",
            "availability_slot",
            ["availability_slot_id"],
            ["id"],
            ondelete="SET NULL",
        )

    booking_indexes = {index["name"] for index in inspector.get_indexes("booking_action")}
    if "ix_booking_action_outreach_job_id" not in booking_indexes:
        op.create_index("ix_booking_action_outreach_job_id", "booking_action", ["outreach_job_id"])
    if "ix_booking_action_availability_slot_id" not in booking_indexes:
        op.create_index(
            "ix_booking_action_availability_slot_id", "booking_action", ["availability_slot_id"]
        )

    booking_uniques = {constraint["name"] for constraint in inspector.get_unique_constraints("booking_action")}
    if "uq_booking_action_clinic_slot" not in booking_uniques:
        op.create_unique_constraint(
            "uq_booking_action_clinic_slot", "booking_action", ["clinic_id", "availability_slot_id"]
        )

    if bind.dialect.name == "postgresql":
        policy = "availability_slot_tenant_isolation"
        predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
        op.execute(sa.text("ALTER TABLE availability_slot ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text("ALTER TABLE availability_slot FORCE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON availability_slot"))
        op.execute(
            sa.text(
                f"CREATE POLICY {policy} ON availability_slot "
                f"USING ({predicate}) WITH CHECK ({predicate})"
            )
        )


def downgrade() -> None:
    """Remove Phase 3 availability and booking idempotency support."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP POLICY IF EXISTS availability_slot_tenant_isolation ON availability_slot"))
        op.execute(sa.text("ALTER TABLE availability_slot DISABLE ROW LEVEL SECURITY"))
    op.drop_constraint("uq_booking_action_clinic_slot", "booking_action", type_="unique")
    op.drop_index("ix_booking_action_availability_slot_id", table_name="booking_action")
    op.drop_index("ix_booking_action_outreach_job_id", table_name="booking_action")
    op.drop_constraint(
        "fk_booking_action_availability_slot_id_availability_slot",
        "booking_action",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_booking_action_outreach_job_id_outreach_job", "booking_action", type_="foreignkey"
    )
    op.drop_column("booking_action", "availability_slot_id")
    op.drop_column("booking_action", "outreach_job_id")
    op.drop_index("ix_availability_slot_appointment_id", table_name="availability_slot")
    op.drop_index("ix_availability_slot_clinician_id", table_name="availability_slot")
    op.drop_index("ix_availability_slot_clinic_start", table_name="availability_slot")
    op.drop_index("ix_availability_slot_clinic_id", table_name="availability_slot")
    op.drop_index("ix_availability_slot_clinic_clinician", table_name="availability_slot")
    op.drop_table("availability_slot")