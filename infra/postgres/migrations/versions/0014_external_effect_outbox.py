"""add tenant-scoped external effect outbox

Revision ID: 0014_external_effect_outbox
Revises: 0013_recall_task_idempotency
Create Date: 2026-07-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import ExternalEffectState, ExternalEffectType
from src.clinic_recall.models import RLS_GUC

revision: str = "0014_external_effect_outbox"
down_revision: str | None = "0013_recall_task_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(py_enum, name: str) -> sa.Enum:
    values = [member.value for member in py_enum]
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(
        py_enum,
        name=name,
        native_enum=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


def upgrade() -> None:
    """Add the first minimized durable provider-effect ledger."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(
            *[member.value for member in ExternalEffectType],
            name="external_effect_type",
        ).create(bind, checkfirst=True)
        postgresql.ENUM(
            *[member.value for member in ExternalEffectState],
            name="external_effect_state",
        ).create(bind, checkfirst=True)

    if "external_effect" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "external_effect",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("clinic_id", sa.String(), nullable=False),
            sa.Column("aggregate_type", sa.String(), nullable=False),
            sa.Column("aggregate_id", sa.String(), nullable=False),
            sa.Column(
                "effect_type",
                _enum(ExternalEffectType, "external_effect_type"),
                nullable=False,
            ),
            sa.Column("idempotency_key", sa.String(), nullable=False),
            sa.Column("payload_version", sa.Integer(), server_default="1", nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "state",
                _enum(ExternalEffectState, "external_effect_state"),
                server_default=ExternalEffectState.PENDING.value,
                nullable=False,
            ),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
            sa.Column("lease_owner", sa.String(), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("provider_resource_id", sa.String(), nullable=True),
            sa.Column("provider_status", sa.String(), nullable=True),
            sa.Column("last_error_class", sa.String(), nullable=True),
            sa.Column("last_error_code", sa.String(), nullable=True),
            sa.Column("completion_evidence_hash", sa.String(length=64), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "clinic_id",
                "effect_type",
                "idempotency_key",
                name="uq_external_effect_logical_request",
            ),
        )
    _ensure_indexes()

    if bind.dialect.name == "postgresql":
        _apply_policy()


def downgrade() -> None:
    """Remove the additive table for local migration compatibility tests."""
    op.drop_table("external_effect")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name="external_effect_state").drop(bind, checkfirst=True)
        postgresql.ENUM(name="external_effect_type").drop(bind, checkfirst=True)


def _apply_policy() -> None:
    policy = "external_effect_tenant_isolation"
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text("ALTER TABLE external_effect ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE external_effect FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON external_effect"))
    op.execute(
        sa.text(
            f"CREATE POLICY {policy} ON external_effect "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )


def _ensure_indexes() -> None:
    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("external_effect")
    }
    required = {
        "ix_external_effect_clinic_id": ["clinic_id"],
        "ix_external_effect_claim": ["clinic_id", "state", "available_at"],
        "ix_external_effect_expired_lease": [
            "clinic_id",
            "state",
            "lease_expires_at",
        ],
    }
    for name, columns in required.items():
        if name not in indexes:
            op.create_index(name, "external_effect", columns)