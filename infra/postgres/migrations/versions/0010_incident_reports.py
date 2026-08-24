"""add anonymous incident reports

Revision ID: 0010_incident_reports
Revises: 0009_inbound_messages
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import (
    IncidentCategory,
    IncidentSeverity,
    IncidentSource,
    IncidentStatus,
)
from src.clinic_recall.models import RLS_GUC

revision: str = "0010_incident_reports"
down_revision: str | None = "0009_inbound_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS = (
    (IncidentSource, "incident_source"),
    (IncidentCategory, "incident_category"),
    (IncidentSeverity, "incident_severity"),
    (IncidentStatus, "incident_status"),
)


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
    """Add the anonymous incident_report table with tenant RLS.

    Anonymity by schema design: no reporter, patient, phone, or IP columns;
    occurrence time is coarsened to the hour by the service layer.
    """
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for py_enum, name in _ENUMS:
            postgresql.ENUM(
                *[member.value for member in py_enum], name=name
            ).create(bind, checkfirst=True)
        # AuditAction gained INCIDENT_REPORT / INCIDENT_STATUS_CHANGE; native
        # Postgres enums must be extended explicitly (ADD VALUE is idempotent
        # with IF NOT EXISTS and cannot run inside the migration transaction
        # on older PG, so use autocommit isolation).
        with op.get_context().autocommit_block():
            for value in ("incident_report", "incident_status_change"):
                op.execute(sa.text(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'"))

    inspector = sa.inspect(bind)
    if "incident_report" not in set(inspector.get_table_names()):
        op.create_table(
            "incident_report",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("clinic_id", sa.String(), nullable=False),
            sa.Column("source", _enum(IncidentSource, "incident_source"), nullable=False),
            sa.Column(
                "category",
                _enum(IncidentCategory, "incident_category"),
                server_default=IncidentCategory.OTHER.value,
                nullable=False,
            ),
            sa.Column(
                "severity",
                _enum(IncidentSeverity, "incident_severity"),
                server_default=IncidentSeverity.NO_HARM.value,
                nullable=False,
            ),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("related_job_id", sa.String(), nullable=True),
            sa.Column(
                "status",
                _enum(IncidentStatus, "incident_status"),
                server_default=IncidentStatus.NEW.value,
                nullable=False,
            ),
            sa.Column("occurred_hour", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
            sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["related_job_id"], ["outreach_job.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )

    _ensure_indexes(sa.inspect(bind))
    if bind.dialect.name == "postgresql":
        _apply_policy("incident_report")


def _ensure_indexes(inspector: sa.Inspector) -> None:
    indexes = {index["name"] for index in inspector.get_indexes("incident_report")}
    if "ix_incident_report_clinic_id" not in indexes:
        op.create_index("ix_incident_report_clinic_id", "incident_report", ["clinic_id"])
    if "ix_incident_report_clinic_status" not in indexes:
        op.create_index(
            "ix_incident_report_clinic_status", "incident_report", ["clinic_id", "status"]
        )
    if "ix_incident_report_clinic_severity" not in indexes:
        op.create_index(
            "ix_incident_report_clinic_severity", "incident_report", ["clinic_id", "severity"]
        )


def _apply_policy(table: str) -> None:
    policy = f"{table}_tenant_isolation"
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
    op.execute(sa.text(f"CREATE POLICY {policy} ON {table} USING ({predicate}) WITH CHECK ({predicate})"))


def downgrade() -> None:
    """Remove the incident_report table and its enums."""
    bind = op.get_bind()
    op.drop_table("incident_report")
    if bind.dialect.name == "postgresql":
        for _, name in reversed(_ENUMS):
            postgresql.ENUM(name=name).drop(bind, checkfirst=True)
