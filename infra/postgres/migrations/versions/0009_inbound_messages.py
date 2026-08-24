"""add minimized inbound sms messages

Revision ID: 0009_inbound_messages
Revises: 0008_inbound_phone_context
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import (
    ClinicPhoneProvider,
    InboundMessageStatus,
    InteractionDirection,
)
from src.clinic_recall.models import RLS_GUC

revision: str = "0009_inbound_messages"
down_revision: str | None = "0008_inbound_phone_context"
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
    """Add minimized inbound SMS records and message-anchored staff tasks."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(
            *[member.value for member in InboundMessageStatus],
            name="inbound_message_status",
        ).create(bind, checkfirst=True)

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "inbound_message" not in tables:
        op.create_table(
            "inbound_message",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("clinic_id", sa.String(), nullable=False),
            sa.Column("clinic_phone_number_id", sa.String(), nullable=True),
            sa.Column("provider", _enum(ClinicPhoneProvider, "clinic_phone_provider"), nullable=False),
            sa.Column("provider_message_id", sa.String(), nullable=False),
            sa.Column("to_number", sa.String(), nullable=False),
            sa.Column("from_number_hash", sa.String(), nullable=True),
            sa.Column(
                "direction",
                _enum(InteractionDirection, "interaction_direction"),
                server_default=InteractionDirection.INBOUND.value,
                nullable=False,
            ),
            sa.Column("body_length", sa.Integer(), nullable=False),
            sa.Column("body_sha256", sa.String(), nullable=False),
            sa.Column("intent", sa.String(), nullable=True),
            sa.Column(
                "status",
                _enum(InboundMessageStatus, "inbound_message_status"),
                server_default=InboundMessageStatus.RECEIVED.value,
                nullable=False,
            ),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["clinic_phone_number_id"], ["clinic_phone_number.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "provider_message_id", name="uq_inbound_message_provider_message"),
        )

    task_columns = {column["name"] for column in inspector.get_columns("inbound_staff_task")}
    task_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("inbound_staff_task")
    }
    with op.batch_alter_table("inbound_staff_task") as batch:
        batch.alter_column("inbound_call_id", existing_type=sa.String(), nullable=True)
        if "inbound_message_id" not in task_columns:
            batch.add_column(sa.Column("inbound_message_id", sa.String(), nullable=True))
            batch.create_foreign_key(
                "fk_inbound_staff_task_inbound_message_id",
                "inbound_message",
                ["inbound_message_id"],
                ["id"],
                ondelete="CASCADE",
            )
        if "ck_inbound_staff_task_one_inbound_anchor" not in task_checks:
            batch.create_check_constraint(
                "ck_inbound_staff_task_one_inbound_anchor",
                "(inbound_call_id IS NOT NULL AND inbound_message_id IS NULL) OR "
                "(inbound_call_id IS NULL AND inbound_message_id IS NOT NULL)",
            )

    _ensure_indexes(sa.inspect(bind))
    if bind.dialect.name == "postgresql":
        _apply_policy("inbound_message")


def _ensure_indexes(inspector: sa.Inspector) -> None:
    message_indexes = {index["name"] for index in inspector.get_indexes("inbound_message")}
    if "ix_inbound_message_clinic_id" not in message_indexes:
        op.create_index("ix_inbound_message_clinic_id", "inbound_message", ["clinic_id"])
    if "ix_inbound_message_clinic_phone_number_id" not in message_indexes:
        op.create_index("ix_inbound_message_clinic_phone_number_id", "inbound_message", ["clinic_phone_number_id"])
    if "ix_inbound_message_from_number_hash" not in message_indexes:
        op.create_index("ix_inbound_message_from_number_hash", "inbound_message", ["from_number_hash"])
    if "ix_inbound_message_clinic_created" not in message_indexes:
        op.create_index("ix_inbound_message_clinic_created", "inbound_message", ["clinic_id", "created_at"])
    if "ix_inbound_message_clinic_status" not in message_indexes:
        op.create_index("ix_inbound_message_clinic_status", "inbound_message", ["clinic_id", "status"])
    if "ix_inbound_message_clinic_intent" not in message_indexes:
        op.create_index("ix_inbound_message_clinic_intent", "inbound_message", ["clinic_id", "intent"])

    task_indexes = {index["name"] for index in inspector.get_indexes("inbound_staff_task")}
    if "ix_inbound_staff_task_inbound_message_id" not in task_indexes:
        op.create_index("ix_inbound_staff_task_inbound_message_id", "inbound_staff_task", ["inbound_message_id"])


def _apply_policy(table: str) -> None:
    policy = f"{table}_tenant_isolation"
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
    op.execute(sa.text(f"CREATE POLICY {policy} ON {table} USING ({predicate}) WITH CHECK ({predicate})"))


def downgrade() -> None:
    """Remove inbound SMS records and restore call-only task anchor."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP POLICY IF EXISTS inbound_message_tenant_isolation ON inbound_message"))
        op.execute(sa.text("ALTER TABLE inbound_message DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_inbound_staff_task_inbound_message_id", table_name="inbound_staff_task")
    with op.batch_alter_table("inbound_staff_task") as batch:
        batch.drop_constraint("ck_inbound_staff_task_one_inbound_anchor", type_="check")
        batch.drop_constraint("fk_inbound_staff_task_inbound_message_id", type_="foreignkey")
        batch.drop_column("inbound_message_id")
        batch.alter_column("inbound_call_id", existing_type=sa.String(), nullable=False)
    op.drop_index("ix_inbound_message_clinic_intent", table_name="inbound_message")
    op.drop_index("ix_inbound_message_clinic_status", table_name="inbound_message")
    op.drop_index("ix_inbound_message_clinic_created", table_name="inbound_message")
    op.drop_index("ix_inbound_message_from_number_hash", table_name="inbound_message")
    op.drop_index("ix_inbound_message_clinic_phone_number_id", table_name="inbound_message")
    op.drop_index("ix_inbound_message_clinic_id", table_name="inbound_message")
    op.drop_table("inbound_message")
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP TYPE IF EXISTS inbound_message_status"))