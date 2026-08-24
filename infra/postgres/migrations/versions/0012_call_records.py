"""add consented call recording + transcript records

Revision ID: 0012_call_records
Revises: 0011_identity_provider
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import (
    CallRecordingStatus,
    ClinicPhoneProvider,
    InteractionDirection,
)
from src.clinic_recall.models import RLS_GUC

revision: str = "0012_call_records"
down_revision: str | None = "0011_identity_provider"
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
    """Add call_record table for consented recordings and minimized transcripts."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(
            *[member.value for member in CallRecordingStatus],
            name="call_recording_status",
        ).create(bind, checkfirst=True)

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "call_record" not in tables:
        op.create_table(
            "call_record",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("clinic_id", sa.String(), nullable=False),
            sa.Column("patient_id", sa.String(), nullable=True),
            sa.Column("provider", _enum(ClinicPhoneProvider, "clinic_phone_provider"), nullable=False),
            sa.Column("provider_call_id", sa.String(), nullable=False),
            sa.Column("session_id", sa.String(), nullable=True),
            sa.Column(
                "direction",
                _enum(InteractionDirection, "interaction_direction"),
                server_default=InteractionDirection.OUTBOUND.value,
                nullable=False,
            ),
            sa.Column("scenario", sa.String(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("outcome", sa.String(), nullable=True),
            sa.Column(
                "recording_status",
                _enum(CallRecordingStatus, "call_recording_status"),
                server_default=CallRecordingStatus.NONE.value,
                nullable=False,
            ),
            sa.Column("recording_sid", sa.String(), nullable=True),
            sa.Column("recording_blob_path", sa.String(), nullable=True),
            sa.Column("recording_duration_s", sa.Integer(), nullable=True),
            sa.Column("transcript", sa.JSON(), nullable=True),
            sa.Column("consent_snapshot", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["patient_id"], ["patient.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "provider_call_id", name="uq_call_record_provider_call"),
        )

    _ensure_indexes(sa.inspect(bind))
    if bind.dialect.name == "postgresql":
        _apply_policy("call_record")


def downgrade() -> None:
    op.drop_table("call_record")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name="call_recording_status").drop(bind, checkfirst=True)


def _ensure_indexes(inspector: sa.Inspector) -> None:
    indexes = {index["name"] for index in inspector.get_indexes("call_record")}
    if "ix_call_record_clinic_id" not in indexes:
        op.create_index("ix_call_record_clinic_id", "call_record", ["clinic_id"])
    if "ix_call_record_patient_id" not in indexes:
        op.create_index("ix_call_record_patient_id", "call_record", ["patient_id"])
    if "ix_call_record_session_id" not in indexes:
        op.create_index("ix_call_record_session_id", "call_record", ["session_id"])
    if "ix_call_record_clinic_created" not in indexes:
        op.create_index("ix_call_record_clinic_created", "call_record", ["clinic_id", "created_at"])
    if "ix_call_record_clinic_status" not in indexes:
        op.create_index("ix_call_record_clinic_status", "call_record", ["clinic_id", "recording_status"])


def _apply_policy(table: str) -> None:
    policy = f"{table}_tenant_isolation"
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
    op.execute(sa.text(f"CREATE POLICY {policy} ON {table} USING ({predicate}) WITH CHECK ({predicate})"))
