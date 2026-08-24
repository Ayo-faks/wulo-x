"""add provider switchable inbound phone context

Revision ID: 0008_inbound_phone_context
Revises: 0007_product_convergence
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import (
    ClinicPhoneProvider,
    ClinicPhonePurpose,
    ClinicPhoneStatus,
    InboundCallStatus,
    InboundStaffTaskKind,
    InboundStaffTaskStatus,
)
from src.clinic_recall.models import RLS_GUC

revision: str = "0008_inbound_phone_context"
down_revision: str | None = "0007_product_convergence"
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
    """Add inbound call routing and anonymous-capable staff tasks."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _create_postgresql_enums(bind)
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "clinic_phone_number" not in tables:
        op.create_table(
            "clinic_phone_number",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("clinic_id", sa.String(), nullable=False),
            sa.Column("phone_number", sa.String(), nullable=False),
            sa.Column("provider", _enum(ClinicPhoneProvider, "clinic_phone_provider"), nullable=False),
            sa.Column("purpose", _enum(ClinicPhonePurpose, "clinic_phone_purpose"), nullable=False),
            sa.Column(
                "status",
                _enum(ClinicPhoneStatus, "clinic_phone_status"),
                server_default=ClinicPhoneStatus.ACTIVE.value,
                nullable=False,
            ),
            sa.Column("config", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "phone_number", name="uq_clinic_phone_provider_number"),
        )

    if "inbound_call" not in tables:
        op.create_table(
            "inbound_call",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("clinic_id", sa.String(), nullable=False),
            sa.Column("clinic_phone_number_id", sa.String(), nullable=True),
            sa.Column("provider", _enum(ClinicPhoneProvider, "clinic_phone_provider"), nullable=False),
            sa.Column("provider_call_id", sa.String(), nullable=False),
            sa.Column("called_number", sa.String(), nullable=False),
            sa.Column("caller_number_hash", sa.String(), nullable=True),
            sa.Column(
                "status",
                _enum(InboundCallStatus, "inbound_call_status"),
                server_default=InboundCallStatus.STARTED.value,
                nullable=False,
            ),
            sa.Column("outcome", sa.String(), nullable=True),
            sa.Column("provider_metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["clinic_phone_number_id"], ["clinic_phone_number.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "provider_call_id", name="uq_inbound_call_provider_call"),
        )

    if "inbound_staff_task" not in tables:
        op.create_table(
            "inbound_staff_task",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("clinic_id", sa.String(), nullable=False),
            sa.Column("inbound_call_id", sa.String(), nullable=False),
            sa.Column("patient_id", sa.String(), nullable=True),
            sa.Column("kind", _enum(InboundStaffTaskKind, "inbound_staff_task_kind"), nullable=False),
            sa.Column(
                "status",
                _enum(InboundStaffTaskStatus, "inbound_staff_task_status"),
                server_default=InboundStaffTaskStatus.OPEN.value,
                nullable=False,
            ),
            sa.Column("priority", sa.String(), server_default="normal", nullable=False),
            sa.Column("reason", sa.String(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("assigned_to", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["inbound_call_id"], ["inbound_call.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["patient_id"], ["patient.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )

    _ensure_indexes(sa.inspect(bind))
    if bind.dialect.name == "postgresql":
        _apply_policy("inbound_call")
        _apply_policy("inbound_staff_task")


def _ensure_indexes(inspector: sa.Inspector) -> None:
    phone_indexes = {index["name"] for index in inspector.get_indexes("clinic_phone_number")}
    if "ix_clinic_phone_number_clinic_id" not in phone_indexes:
        op.create_index("ix_clinic_phone_number_clinic_id", "clinic_phone_number", ["clinic_id"])
    if "ix_clinic_phone_number_clinic_status" not in phone_indexes:
        op.create_index("ix_clinic_phone_number_clinic_status", "clinic_phone_number", ["clinic_id", "status"])
    if "ix_clinic_phone_number_provider_purpose" not in phone_indexes:
        op.create_index(
            "ix_clinic_phone_number_provider_purpose",
            "clinic_phone_number",
            ["provider", "purpose", "status"],
        )

    call_indexes = {index["name"] for index in inspector.get_indexes("inbound_call")}
    if "ix_inbound_call_clinic_id" not in call_indexes:
        op.create_index("ix_inbound_call_clinic_id", "inbound_call", ["clinic_id"])
    if "ix_inbound_call_clinic_phone_number_id" not in call_indexes:
        op.create_index("ix_inbound_call_clinic_phone_number_id", "inbound_call", ["clinic_phone_number_id"])
    if "ix_inbound_call_caller_number_hash" not in call_indexes:
        op.create_index("ix_inbound_call_caller_number_hash", "inbound_call", ["caller_number_hash"])
    if "ix_inbound_call_clinic_created" not in call_indexes:
        op.create_index("ix_inbound_call_clinic_created", "inbound_call", ["clinic_id", "created_at"])
    if "ix_inbound_call_clinic_status" not in call_indexes:
        op.create_index("ix_inbound_call_clinic_status", "inbound_call", ["clinic_id", "status"])

    task_indexes = {index["name"] for index in inspector.get_indexes("inbound_staff_task")}
    if "ix_inbound_staff_task_clinic_id" not in task_indexes:
        op.create_index("ix_inbound_staff_task_clinic_id", "inbound_staff_task", ["clinic_id"])
    if "ix_inbound_staff_task_inbound_call_id" not in task_indexes:
        op.create_index("ix_inbound_staff_task_inbound_call_id", "inbound_staff_task", ["inbound_call_id"])
    if "ix_inbound_staff_task_patient_id" not in task_indexes:
        op.create_index("ix_inbound_staff_task_patient_id", "inbound_staff_task", ["patient_id"])
    if "ix_inbound_staff_task_clinic_status" not in task_indexes:
        op.create_index("ix_inbound_staff_task_clinic_status", "inbound_staff_task", ["clinic_id", "status"])
    if "ix_inbound_staff_task_clinic_kind" not in task_indexes:
        op.create_index("ix_inbound_staff_task_clinic_kind", "inbound_staff_task", ["clinic_id", "kind"])


def _create_postgresql_enums(bind) -> None:
    for py_enum, name in (
        (ClinicPhoneProvider, "clinic_phone_provider"),
        (ClinicPhonePurpose, "clinic_phone_purpose"),
        (ClinicPhoneStatus, "clinic_phone_status"),
        (InboundCallStatus, "inbound_call_status"),
        (InboundStaffTaskKind, "inbound_staff_task_kind"),
        (InboundStaffTaskStatus, "inbound_staff_task_status"),
    ):
        postgresql.ENUM(
            *[member.value for member in py_enum],
            name=name,
        ).create(bind, checkfirst=True)


def _apply_policy(table: str) -> None:
    policy = f"{table}_tenant_isolation"
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
    op.execute(sa.text(f"CREATE POLICY {policy} ON {table} USING ({predicate}) WITH CHECK ({predicate})"))


def downgrade() -> None:
    """Remove inbound phone context tables."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in ("inbound_staff_task", "inbound_call"):
            op.execute(sa.text(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}"))
            op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
    op.drop_index("ix_inbound_staff_task_clinic_kind", table_name="inbound_staff_task")
    op.drop_index("ix_inbound_staff_task_clinic_status", table_name="inbound_staff_task")
    op.drop_index("ix_inbound_staff_task_patient_id", table_name="inbound_staff_task")
    op.drop_index("ix_inbound_staff_task_inbound_call_id", table_name="inbound_staff_task")
    op.drop_index("ix_inbound_staff_task_clinic_id", table_name="inbound_staff_task")
    op.drop_table("inbound_staff_task")
    op.drop_index("ix_inbound_call_clinic_status", table_name="inbound_call")
    op.drop_index("ix_inbound_call_clinic_created", table_name="inbound_call")
    op.drop_index("ix_inbound_call_caller_number_hash", table_name="inbound_call")
    op.drop_index("ix_inbound_call_clinic_phone_number_id", table_name="inbound_call")
    op.drop_index("ix_inbound_call_clinic_id", table_name="inbound_call")
    op.drop_table("inbound_call")
    op.drop_index("ix_clinic_phone_number_provider_purpose", table_name="clinic_phone_number")
    op.drop_index("ix_clinic_phone_number_clinic_status", table_name="clinic_phone_number")
    op.drop_index("ix_clinic_phone_number_clinic_id", table_name="clinic_phone_number")
    op.drop_table("clinic_phone_number")
    if bind.dialect.name == "postgresql":
        for enum_name in (
            "inbound_staff_task_status",
            "inbound_staff_task_kind",
            "inbound_call_status",
            "clinic_phone_status",
            "clinic_phone_purpose",
            "clinic_phone_provider",
        ):
            op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))