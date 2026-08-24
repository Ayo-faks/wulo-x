"""add server-owned identity evidence tiers

Revision ID: 0023_identity_evidence_tiers
Revises: 0022_cliniko_booking_effect
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import (
    Channel,
    IdentityEvidenceReason,
    IdentityEvidenceState,
    IdentityFactorResult,
    IdentityTier,
)
from src.clinic_recall.models import RLS_GUC

revision: str = "0023_identity_evidence_tiers"
down_revision: str | None = "0022_cliniko_booking_effect"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("identity_evidence", "identity_factor_attempt")
_NEW_ENUMS = (
    (IdentityTier, "identity_tier"),
    (IdentityEvidenceState, "identity_evidence_state"),
    (IdentityEvidenceReason, "identity_evidence_reason"),
    (IdentityFactorResult, "identity_factor_result"),
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
    """Add minimized evidence metadata without any raw identity values."""
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names()) & set(_TABLES)
    if existing and existing != set(_TABLES):
        raise RuntimeError("0023 found a partial identity evidence schema")
    if bind.dialect.name == "postgresql":
        for py_enum, name in _NEW_ENUMS:
            postgresql.ENUM(
                *[member.value for member in py_enum],
                name=name,
            ).create(bind, checkfirst=True)
    if not existing:
        _create_identity_evidence()
        _create_identity_factor_attempt()
    _ensure_booking_action_binding()
    _ensure_indexes()
    if bind.dialect.name == "postgresql":
        for table in _TABLES:
            _apply_policy(table)


def downgrade() -> None:
    """Remove empty local schema only after every grant has been revoked."""
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    retained_tables = [table for table in _TABLES if table in tables]
    if bind.dialect.name == "postgresql":
        for table in retained_tables:
            op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
    retained = 0
    for table in retained_tables:
        retained += int(bind.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)
    if retained:
        if bind.dialect.name == "postgresql":
            for table in retained_tables:
                op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        raise RuntimeError("revoke identity evidence before downgrade")
    _drop_booking_action_binding()
    for table in reversed(_TABLES):
        if table in tables:
            op.drop_table(table)
    if bind.dialect.name == "postgresql":
        for _py_enum, name in reversed(_NEW_ENUMS):
            postgresql.ENUM(name=name).drop(bind, checkfirst=True)


def _create_identity_evidence() -> None:
    op.create_table(
        "identity_evidence",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("clinic_id", sa.String(), nullable=False),
        sa.Column("session_key_hash", sa.String(length=64), nullable=False),
        sa.Column("route_key_hash", sa.String(length=64), nullable=False),
        sa.Column("patient_key_hash", sa.String(length=64), nullable=False),
        sa.Column("channel", _enum(Channel, "channel"), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column(
            "tier",
            _enum(IdentityTier, "identity_tier"),
            server_default=IdentityTier.T0.value,
            nullable=False,
        ),
        sa.Column(
            "state",
            _enum(IdentityEvidenceState, "identity_evidence_state"),
            server_default=IdentityEvidenceState.ACTIVE.value,
            nullable=False,
        ),
        sa.Column(
            "reason",
            _enum(IdentityEvidenceReason, "identity_evidence_reason"),
            server_default=IdentityEvidenceReason.ROUTE_ONLY.value,
            nullable=False,
        ),
        sa.Column(
            "matched_factor_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "dob_verified",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("challenge_token_hash", sa.String(length=64), nullable=True),
        sa.Column("challenge_consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_factor_type", sa.String(length=64), nullable=True),
        sa.Column(
            "revision",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
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
            "length(session_key_hash) = 64",
            name="ck_identity_evidence_session_hash",
        ),
        sa.CheckConstraint(
            "length(route_key_hash) = 64",
            name="ck_identity_evidence_route_hash",
        ),
        sa.CheckConstraint(
            "length(patient_key_hash) = 64",
            name="ck_identity_evidence_patient_hash",
        ),
        sa.CheckConstraint(
            "challenge_token_hash IS NULL OR length(challenge_token_hash) = 64",
            name="ck_identity_evidence_challenge_hash",
        ),
        sa.CheckConstraint(
            "matched_factor_count >= 0 AND attempt_count >= 0 "
            "AND max_attempts >= 1 AND attempt_count <= max_attempts",
            name="ck_identity_evidence_attempt_bounds",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_identity_evidence_expiry_order",
        ),
        sa.CheckConstraint(
            "tier <> 't2' OR (matched_factor_count >= 2 AND dob_verified = true)",
            name="ck_identity_evidence_t2_factors",
        ),
        sa.CheckConstraint(
            "state <> 'revoked' OR revoked_at IS NOT NULL",
            name="ck_identity_evidence_revocation_time",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_identity_evidence_clinic_id_id",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "session_key_hash",
            name="uq_identity_evidence_session",
        ),
    )


def _create_identity_factor_attempt() -> None:
    op.create_table(
        "identity_factor_attempt",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("clinic_id", sa.String(), nullable=False),
        sa.Column("evidence_id", sa.String(), nullable=False),
        sa.Column("factor_type", sa.String(length=64), nullable=False),
        sa.Column(
            "result",
            _enum(IdentityFactorResult, "identity_factor_result"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
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
            "length(factor_type) BETWEEN 1 AND 64",
            name="ck_identity_factor_attempt_type_length",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_identity_factor_attempt_number_positive",
        ),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinic.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["clinic_id", "evidence_id"],
            ["identity_evidence.clinic_id", "identity_evidence.id"],
            ondelete="CASCADE",
            name="fk_identity_factor_attempt_evidence_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "clinic_id",
            "id",
            name="uq_identity_factor_attempt_clinic_id_id",
        ),
        sa.UniqueConstraint(
            "clinic_id",
            "evidence_id",
            "attempt_number",
            name="uq_identity_factor_attempt_number",
        ),
    )


def _ensure_indexes() -> None:
    required = {
        "identity_evidence": {
            "ix_identity_evidence_clinic_id": ["clinic_id"],
            "ix_identity_evidence_clinic_expiry": [
                "clinic_id",
                "state",
                "expires_at",
            ],
        },
        "identity_factor_attempt": {
            "ix_identity_factor_attempt_clinic_id": ["clinic_id"],
            "ix_identity_factor_attempt_evidence": ["clinic_id", "evidence_id"],
        },
    }
    inspector = sa.inspect(op.get_bind())
    for table, indexes in required.items():
        existing = {index["name"] for index in inspector.get_indexes(table)}
        for name, columns in indexes.items():
            if name not in existing:
                op.create_index(name, table, columns)


def _ensure_booking_action_binding() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("booking_action")
    }
    required = {
        "identity_evidence_id",
        "identity_policy_version",
        "identity_evidence_revision",
    }
    present = columns & required
    if present and present != required:
        raise RuntimeError("0023 found a partial booking identity binding")
    foreign_keys = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_foreign_keys("booking_action")
        if constraint.get("name")
    }
    checks = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("booking_action")
        if constraint.get("name")
    }
    with op.batch_alter_table("booking_action") as batch:
        if not present:
            batch.add_column(
                sa.Column("identity_evidence_id", sa.String(), nullable=True)
            )
            batch.add_column(
                sa.Column("identity_policy_version", sa.String(length=128), nullable=True)
            )
            batch.add_column(
                sa.Column("identity_evidence_revision", sa.Integer(), nullable=True)
            )
        if "fk_booking_action_identity_evidence_tenant" not in foreign_keys:
            batch.create_foreign_key(
                "fk_booking_action_identity_evidence_tenant",
                "identity_evidence",
                ["clinic_id", "identity_evidence_id"],
                ["clinic_id", "id"],
                ondelete="RESTRICT",
            )
        if "ck_booking_action_identity_binding_complete" not in checks:
            batch.create_check_constraint(
                "ck_booking_action_identity_binding_complete",
                "(identity_evidence_id IS NULL AND identity_policy_version IS NULL "
                "AND identity_evidence_revision IS NULL) OR "
                "(identity_evidence_id IS NOT NULL AND identity_policy_version IS NOT NULL "
                "AND identity_evidence_revision >= 0)",
            )
    indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("booking_action")
    }
    if "ix_booking_action_identity_evidence_id" not in indexes:
        op.create_index(
            "ix_booking_action_identity_evidence_id",
            "booking_action",
            ["identity_evidence_id"],
        )


def _drop_booking_action_binding() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("booking_action")
    }
    required = (
        "identity_evidence_revision",
        "identity_policy_version",
        "identity_evidence_id",
    )
    if not (columns & set(required)):
        return
    indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("booking_action")
    }
    foreign_keys = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_foreign_keys("booking_action")
        if constraint.get("name")
    }
    checks = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("booking_action")
        if constraint.get("name")
    }
    if "ix_booking_action_identity_evidence_id" in indexes:
        op.drop_index("ix_booking_action_identity_evidence_id", table_name="booking_action")
    with op.batch_alter_table("booking_action") as batch:
        if "fk_booking_action_identity_evidence_tenant" in foreign_keys:
            batch.drop_constraint(
                "fk_booking_action_identity_evidence_tenant",
                type_="foreignkey",
            )
        if "ck_booking_action_identity_binding_complete" in checks:
            batch.drop_constraint(
                "ck_booking_action_identity_binding_complete",
                type_="check",
            )
        for column in required:
            if column in columns:
                batch.drop_column(column)


def _apply_policy(table: str) -> None:
    policy = f"{table}_tenant_isolation"
    predicate = f"clinic_id = current_setting('{RLS_GUC}', true)"
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
    op.execute(
        sa.text(
            f"CREATE POLICY {policy} ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
    )