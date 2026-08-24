"""add tenant cadence cursor and dead-letter handoff

Revision ID: 0016_scheduled_cadence
Revises: 0015_provider_callback_receipts
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from src.clinic_recall.models import RLS_GUC

revision: str = "0016_scheduled_cadence"
down_revision: str | None = "0015_provider_callback_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add minimized PR-03 cursor and queued handoff receipts."""
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "cadence_cursor" not in tables:
        _create_cadence_cursor()
    if "external_effect_handoff" not in tables:
        _create_external_effect_handoff()
    _ensure_indexes()
    if bind.dialect.name == "postgresql":
        _apply_policy("cadence_cursor", "cadence_cursor_tenant_isolation")
        _apply_policy(
            "external_effect_handoff",
            "external_effect_handoff_tenant_isolation",
        )


def downgrade() -> None:
    """Remove local PR-03 tables while retaining durable effects and callbacks."""
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "external_effect_handoff" in tables:
        op.drop_table("external_effect_handoff")
    if "cadence_cursor" in tables:
        op.drop_table("cadence_cursor")


def _create_cadence_cursor() -> None:
    op.create_table(
        "cadence_cursor",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("clinic_id", sa.String(), nullable=False),
        sa.Column("planner_name", sa.String(length=64), nullable=False),
        sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_id", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint(
            "length(planner_name) BETWEEN 1 AND 64",
            name="ck_cadence_cursor_planner_name_length",
        ),
        sa.CheckConstraint(
            "last_run_id IS NULL OR length(last_run_id) BETWEEN 1 AND 64",
            name="ck_cadence_cursor_run_id_length",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "clinic_id",
            "planner_name",
            name="uq_cadence_cursor_clinic_planner",
        ),
    )


def _create_external_effect_handoff() -> None:
    op.create_table(
        "external_effect_handoff",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("clinic_id", sa.String(), nullable=False),
        sa.Column("external_effect_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
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
        sa.CheckConstraint(
            "status = 'queued'",
            name="ck_external_effect_handoff_status_queued",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "external_effect_id"],
            ["external_effect.clinic_id", "external_effect.id"],
            ondelete="RESTRICT",
            name="fk_external_effect_handoff_effect_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "clinic_id",
            "external_effect_id",
            name="uq_external_effect_handoff_effect",
        ),
    )


def _ensure_indexes() -> None:
    required = {
        "cadence_cursor": {
            "ix_cadence_cursor_clinic_id": ["clinic_id"],
            "ix_cadence_cursor_clinic_watermark": ["clinic_id", "watermark_at"],
        },
        "external_effect_handoff": {
            "ix_external_effect_handoff_clinic_id": ["clinic_id"],
            "ix_external_effect_handoff_clinic_status": ["clinic_id", "status"],
        },
    }
    bind = op.get_bind()
    for table_name, indexes in required.items():
        existing = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
        for name, columns in indexes.items():
            if name not in existing:
                op.create_index(name, table_name, columns)


def _apply_policy(table_name: str, policy: str) -> None:
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table_name}"))
    op.execute(
        sa.text(
            f"CREATE POLICY {policy} ON {table_name} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )
