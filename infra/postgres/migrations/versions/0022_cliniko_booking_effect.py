"""add bounded durable Cliniko booking effect state

Revision ID: 0022_cliniko_booking_effect
Revises: 0021_controlled_csv_import
Create Date: 2026-07-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_cliniko_booking_effect"
down_revision: str | None = "0021_controlled_csv_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_READ_COLUMNS = frozenset(
    {
        "read_attempt_count",
        "max_read_attempts",
        "settle_deadline_at",
        "preflight_evidence_hash",
    }
)
_READ_BOUNDS = "ck_external_effect_read_attempt_bounds"
_PREFLIGHT_HASH = "ck_external_effect_preflight_evidence_hash"
_DOWNGRADE_EFFECT_GUARD = "ck_external_effect_pr07_downgrade_safe"
_DOWNGRADE_ACTION_GUARD = "ck_booking_action_pr07_downgrade_safe"


def upgrade() -> None:
    """Add one closed effect kind and persisted bounded-read authority."""
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("external_effect")}
    present = existing & _READ_COLUMNS
    if present not in {frozenset(), _READ_COLUMNS}:
        raise RuntimeError("0021 found a partial Cliniko booking schema")
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                sa.text(
                    "ALTER TYPE external_effect_type "
                    "ADD VALUE IF NOT EXISTS 'cliniko_booking'"
                )
            )
    if not present:
        op.add_column(
            "external_effect",
            sa.Column(
                "read_attempt_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        op.add_column(
            "external_effect",
            sa.Column(
                "max_read_attempts",
                sa.Integer(),
                nullable=False,
                server_default="4",
            ),
        )
        op.add_column(
            "external_effect",
            sa.Column("settle_deadline_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            "external_effect",
            sa.Column("preflight_evidence_hash", sa.String(64), nullable=True),
        )
    if bind.dialect.name == "postgresql":
        _install_postgres_constraints()
    else:
        _install_sqlite_guards()


def downgrade() -> None:
    """Remove read columns only when no PR-07 provider state exists."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _validate_postgres_downgrade_state()
        _drop_postgres_constraints()
    else:
        unsafe_effects = bind.scalar(
            sa.text(
                "SELECT count(*) FROM external_effect "
                "WHERE effect_type = 'cliniko_booking'"
            )
        )
        unsafe_actions = bind.scalar(
            sa.text(
                "SELECT count(*) FROM booking_action WHERE "
                "write_back_state <> 'not_attempted' OR written_back = true OR "
                "external_appointment_ref IS NOT NULL OR provider_attempted_at IS NOT NULL OR "
                "read_back_verified_at IS NOT NULL OR conflict_reason IS NOT NULL"
            )
        )
        if unsafe_effects or unsafe_actions:
            raise RuntimeError("disable Cliniko booking state before downgrade")
        _drop_sqlite_guards()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("external_effect")}
    if bind.dialect.name == "sqlite":
        checks = {
            constraint["name"]
            for constraint in sa.inspect(bind).get_check_constraints("external_effect")
        }
        with op.batch_alter_table("external_effect", recreate="always") as batch:
            for name in (_PREFLIGHT_HASH, _READ_BOUNDS):
                if name in checks:
                    batch.drop_constraint(name, type_="check")
            for column in (
                "preflight_evidence_hash",
                "settle_deadline_at",
                "max_read_attempts",
                "read_attempt_count",
            ):
                if column in existing:
                    batch.drop_column(column)
    else:
        for column in (
            "preflight_evidence_hash",
            "settle_deadline_at",
            "max_read_attempts",
            "read_attempt_count",
        ):
            if column in existing:
                op.drop_column("external_effect", column)


def _install_postgres_constraints() -> None:
    existing = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("external_effect")
    }
    conditions = {
        _READ_BOUNDS: (
            "read_attempt_count >= 0 AND max_read_attempts >= 1 "
            "AND read_attempt_count <= max_read_attempts"
        ),
        _PREFLIGHT_HASH: (
            "preflight_evidence_hash IS NULL OR length(preflight_evidence_hash) = 64"
        ),
    }
    for name, condition in conditions.items():
        if name not in existing:
            op.create_check_constraint(name, "external_effect", condition)


def _drop_postgres_constraints() -> None:
    existing = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("external_effect")
    }
    for name in (_PREFLIGHT_HASH, _READ_BOUNDS):
        if name in existing:
            op.drop_constraint(name, "external_effect", type_="check")


def _validate_postgres_downgrade_state() -> None:
    savepoint = op.get_bind().begin_nested()
    try:
        op.create_check_constraint(
            _DOWNGRADE_EFFECT_GUARD,
            "external_effect",
            "effect_type <> 'cliniko_booking'",
        )
        op.create_check_constraint(
            _DOWNGRADE_ACTION_GUARD,
            "booking_action",
            "write_back_state = 'not_attempted' AND written_back = false "
            "AND external_appointment_ref IS NULL AND provider_attempted_at IS NULL "
            "AND read_back_verified_at IS NULL AND conflict_reason IS NULL",
        )
    except sa.exc.DBAPIError:
        savepoint.rollback()
        raise RuntimeError("disable Cliniko booking state before downgrade") from None
    savepoint.commit()
    op.drop_constraint(_DOWNGRADE_EFFECT_GUARD, "external_effect", type_="check")
    op.drop_constraint(_DOWNGRADE_ACTION_GUARD, "booking_action", type_="check")


def _install_sqlite_guards() -> None:
    _drop_sqlite_guards()
    condition = (
        "NEW.read_attempt_count < 0 OR NEW.max_read_attempts < 1 OR "
        "NEW.read_attempt_count > NEW.max_read_attempts OR "
        "(NEW.preflight_evidence_hash IS NOT NULL "
        "AND length(NEW.preflight_evidence_hash) <> 64)"
    )
    for operation in ("INSERT", "UPDATE"):
        suffix = operation.lower()
        op.execute(
            sa.text(
                f"CREATE TRIGGER external_effect_pr07_bounds_{suffix} "
                f"BEFORE {operation} ON external_effect WHEN {condition} "
                "BEGIN SELECT RAISE(ABORT, 'invalid Cliniko read bounds'); END"
            )
        )


def _drop_sqlite_guards() -> None:
    for name in (
        "external_effect_pr07_bounds_insert",
        "external_effect_pr07_bounds_update",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))