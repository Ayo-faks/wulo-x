"""add authoritative availability observations and booking write-back state

Revision ID: 0020_availability_booking_state
Revises: 0019_rights_retention_purge
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from src.clinic_recall.enums import BookingWriteBackState

revision: str = "0020_availability_booking_state"
down_revision: str | None = "0019_rights_retention_purge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AVAILABILITY_COLUMNS = frozenset(
    {
        "source_provider",
        "business_id",
        "appointment_type_id",
        "fetched_at",
        "expires_at",
    }
)
_BOOKING_COLUMNS = frozenset(
    {
        "write_back_state",
        "external_appointment_ref",
        "request_hash",
        "provider_attempted_at",
        "read_back_verified_at",
        "conflict_reason",
    }
)
_WRITE_BACK_ENUM = "booking_write_back_state"
_FRESH_INDEX = "ix_availability_slot_clinic_fresh"
_LEGACY_WRITTEN_BACK_GUARD = "ck_booking_action_pr06_legacy_written_back_false"
_DOWNGRADE_GUARD = "ck_booking_action_pr06_downgrade_safe"


def _write_back_type() -> sa.types.TypeEngine:
    values = [state.value for state in BookingWriteBackState]
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=_WRITE_BACK_ENUM, create_type=False)
    return sa.String(32)


def upgrade() -> None:
    """Add compatibility-safe observation and provider-result state."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    availability_existing = {
        column["name"] for column in inspector.get_columns("availability_slot")
    }
    booking_existing = {
        column["name"] for column in inspector.get_columns("booking_action")
    }
    availability_present = availability_existing & _AVAILABILITY_COLUMNS
    booking_present = booking_existing & _BOOKING_COLUMNS
    if availability_present not in {frozenset(), _AVAILABILITY_COLUMNS}:
        raise RuntimeError("0020 found a partial availability schema; repair before replay")
    if booking_present not in {frozenset(), _BOOKING_COLUMNS}:
        raise RuntimeError("0020 found a partial booking schema; repair before replay")
    schema_exists = (
        availability_present == _AVAILABILITY_COLUMNS
        and booking_present == _BOOKING_COLUMNS
    )
    if bind.dialect.name == "postgresql" and not schema_exists:
        _install_legacy_written_back_guard()
    elif bind.dialect.name != "postgresql":
        legacy_written = bind.scalar(
            sa.text("SELECT count(*) FROM booking_action WHERE written_back = true")
        )
        if legacy_written:
            raise RuntimeError("legacy written_back rows require review before migration")
    if not schema_exists:
        if bind.dialect.name == "postgresql":
            postgresql.ENUM(
                *[state.value for state in BookingWriteBackState],
                name=_WRITE_BACK_ENUM,
            ).create(bind, checkfirst=True)
        _add_columns()

    _create_fresh_index()
    if bind.dialect.name == "postgresql":
        _install_postgres_constraints()
        _drop_legacy_written_back_guard()
        for table in ("availability_slot", "booking_action"):
            op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    else:
        _install_sqlite_guards()


def downgrade() -> None:
    """Return to the 0019 compatibility schema without changing local status."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    booking_existing = {
        column["name"] for column in inspector.get_columns("booking_action")
    }
    if "write_back_state" in booking_existing:
        if bind.dialect.name == "postgresql":
            _validate_postgres_downgrade_state()
        else:
            non_compatible = bind.scalar(
                sa.text(
                    "SELECT count(*) FROM booking_action WHERE "
                    "write_back_state <> 'not_attempted' OR written_back = true OR "
                    "external_appointment_ref IS NOT NULL OR "
                    "provider_attempted_at IS NOT NULL OR "
                    "read_back_verified_at IS NOT NULL OR conflict_reason IS NOT NULL"
                )
            )
            if non_compatible:
                raise RuntimeError("disable provider write-back state before downgrade")

    if bind.dialect.name == "postgresql":
        _drop_postgres_constraints()
    else:
        _drop_sqlite_guards()
    if _FRESH_INDEX in {
        index["name"] for index in sa.inspect(bind).get_indexes("availability_slot")
    }:
        op.drop_index(_FRESH_INDEX, table_name="availability_slot")

    if bind.dialect.name == "sqlite":
        _drop_sqlite_columns()
    else:
        for column in (
            "conflict_reason",
            "read_back_verified_at",
            "provider_attempted_at",
            "request_hash",
            "external_appointment_ref",
            "write_back_state",
        ):
            if column in {
                item["name"] for item in sa.inspect(bind).get_columns("booking_action")
            }:
                op.drop_column("booking_action", column)
        for column in (
            "expires_at",
            "fetched_at",
            "appointment_type_id",
            "business_id",
            "source_provider",
        ):
            if column in {
                item["name"]
                for item in sa.inspect(bind).get_columns("availability_slot")
            }:
                op.drop_column("availability_slot", column)
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name=_WRITE_BACK_ENUM).drop(bind, checkfirst=True)


def _add_columns() -> None:
    op.add_column(
        "availability_slot",
        sa.Column("source_provider", sa.String(32), nullable=True),
    )
    op.add_column(
        "availability_slot",
        sa.Column("business_id", sa.String(), nullable=True),
    )
    op.add_column(
        "availability_slot",
        sa.Column("appointment_type_id", sa.String(), nullable=True),
    )
    op.add_column(
        "availability_slot",
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "availability_slot",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "booking_action",
        sa.Column(
            "write_back_state",
            _write_back_type(),
            nullable=False,
            server_default=BookingWriteBackState.NOT_ATTEMPTED.value,
        ),
    )
    op.add_column(
        "booking_action",
        sa.Column("external_appointment_ref", sa.String(200), nullable=True),
    )
    op.add_column(
        "booking_action",
        sa.Column("request_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "booking_action",
        sa.Column("provider_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "booking_action",
        sa.Column("read_back_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "booking_action",
        sa.Column("conflict_reason", sa.String(64), nullable=True),
    )


def _create_fresh_index() -> None:
    bind = op.get_bind()
    indexes = {
        index["name"] for index in sa.inspect(bind).get_indexes("availability_slot")
    }
    if _FRESH_INDEX not in indexes:
        op.create_index(
            _FRESH_INDEX,
            "availability_slot",
            ["clinic_id", "expires_at", "start_at"],
        )


def _install_postgres_constraints() -> None:
    existing_availability = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(
            "availability_slot"
        )
    }
    if "ck_availability_slot_authoritative_observation" not in existing_availability:
        op.create_check_constraint(
            "ck_availability_slot_authoritative_observation",
            "availability_slot",
            "(source_provider IS NULL AND business_id IS NULL "
            "AND appointment_type_id IS NULL AND fetched_at IS NULL "
            "AND expires_at IS NULL) OR "
            "(source_provider IS NOT NULL AND business_id IS NOT NULL "
            "AND clinician_id IS NOT NULL AND appointment_type_id IS NOT NULL "
            "AND fetched_at IS NOT NULL AND expires_at IS NOT NULL "
            "AND expires_at > fetched_at)",
        )
    existing_booking = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("booking_action")
    }
    constraints = {
        "ck_booking_action_request_hash_length": (
            "request_hash IS NULL OR length(request_hash) = 64"
        ),
        "ck_booking_action_conflict_reason_length": (
            "conflict_reason IS NULL OR length(conflict_reason) BETWEEN 1 AND 64"
        ),
        "ck_booking_action_verified_write_back": (
            "(write_back_state = 'verified' AND written_back = true "
            "AND external_appointment_ref IS NOT NULL "
            "AND provider_attempted_at IS NOT NULL "
            "AND read_back_verified_at IS NOT NULL) OR "
            "(write_back_state <> 'verified' AND written_back = false "
            "AND read_back_verified_at IS NULL)"
        ),
    }
    for name, condition in constraints.items():
        if name not in existing_booking:
            op.create_check_constraint(name, "booking_action", condition)


def _install_legacy_written_back_guard() -> None:
    existing = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(
            "booking_action"
        )
    }
    if _LEGACY_WRITTEN_BACK_GUARD in existing:
        return
    try:
        op.create_check_constraint(
            _LEGACY_WRITTEN_BACK_GUARD,
            "booking_action",
            "written_back = false",
        )
    except sa.exc.DBAPIError:
        raise RuntimeError(
            "legacy written_back rows require review before migration"
        ) from None


def _drop_legacy_written_back_guard() -> None:
    existing = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(
            "booking_action"
        )
    }
    if _LEGACY_WRITTEN_BACK_GUARD in existing:
        op.drop_constraint(
            _LEGACY_WRITTEN_BACK_GUARD,
            "booking_action",
            type_="check",
        )


def _validate_postgres_downgrade_state() -> None:
    try:
        op.create_check_constraint(
            _DOWNGRADE_GUARD,
            "booking_action",
            "write_back_state = 'not_attempted' AND written_back = false "
            "AND external_appointment_ref IS NULL "
            "AND provider_attempted_at IS NULL "
            "AND read_back_verified_at IS NULL "
            "AND conflict_reason IS NULL",
        )
    except sa.exc.DBAPIError:
        raise RuntimeError("disable provider write-back state before downgrade") from None
    op.drop_constraint(
        _DOWNGRADE_GUARD,
        "booking_action",
        type_="check",
    )


def _drop_postgres_constraints() -> None:
    bind = op.get_bind()
    for table, names in (
        (
            "booking_action",
            (
                "ck_booking_action_verified_write_back",
                "ck_booking_action_conflict_reason_length",
                "ck_booking_action_request_hash_length",
            ),
        ),
        (
            "availability_slot",
            ("ck_availability_slot_authoritative_observation",),
        ),
    ):
        existing = {
            constraint["name"]
            for constraint in sa.inspect(bind).get_check_constraints(table)
        }
        for name in names:
            if name in existing:
                op.drop_constraint(name, table, type_="check")


def _drop_sqlite_columns() -> None:
    bind = op.get_bind()
    booking_checks = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("booking_action")
    }
    with op.batch_alter_table("booking_action", recreate="always") as batch:
        for name in (
            "ck_booking_action_verified_write_back",
            "ck_booking_action_conflict_reason_length",
            "ck_booking_action_request_hash_length",
        ):
            if name in booking_checks:
                batch.drop_constraint(name, type_="check")
        for column in (
            "conflict_reason",
            "read_back_verified_at",
            "provider_attempted_at",
            "request_hash",
            "external_appointment_ref",
            "write_back_state",
        ):
            batch.drop_column(column)

    availability_checks = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("availability_slot")
    }
    with op.batch_alter_table("availability_slot", recreate="always") as batch:
        if "ck_availability_slot_authoritative_observation" in availability_checks:
            batch.drop_constraint(
                "ck_availability_slot_authoritative_observation",
                type_="check",
            )
        for column in (
            "expires_at",
            "fetched_at",
            "appointment_type_id",
            "business_id",
            "source_provider",
        ):
            batch.drop_column(column)


def _install_sqlite_guards() -> None:
    _drop_sqlite_guards()
    availability_condition = (
        "NOT ((NEW.source_provider IS NULL AND NEW.business_id IS NULL "
        "AND NEW.appointment_type_id IS NULL AND NEW.fetched_at IS NULL "
        "AND NEW.expires_at IS NULL) OR "
        "(NEW.source_provider IS NOT NULL AND NEW.business_id IS NOT NULL "
        "AND NEW.clinician_id IS NOT NULL AND NEW.appointment_type_id IS NOT NULL "
        "AND NEW.fetched_at IS NOT NULL AND NEW.expires_at IS NOT NULL "
        "AND NEW.expires_at > NEW.fetched_at))"
    )
    booking_condition = (
        "NOT (((NEW.write_back_state = 'verified') AND NEW.written_back = 1 "
        "AND NEW.external_appointment_ref IS NOT NULL "
        "AND NEW.provider_attempted_at IS NOT NULL "
        "AND NEW.read_back_verified_at IS NOT NULL) OR "
        "((NEW.write_back_state <> 'verified') AND NEW.written_back = 0 "
        "AND NEW.read_back_verified_at IS NULL)) OR "
        "(NEW.request_hash IS NOT NULL AND length(NEW.request_hash) <> 64) OR "
        "(NEW.conflict_reason IS NOT NULL AND "
        "length(NEW.conflict_reason) NOT BETWEEN 1 AND 64)"
    )
    for operation in ("INSERT", "UPDATE"):
        suffix = operation.lower()
        op.execute(
            sa.text(
                f"CREATE TRIGGER availability_slot_authoritative_observation_{suffix} "
                f"BEFORE {operation} ON availability_slot WHEN {availability_condition} "
                "BEGIN SELECT RAISE(ABORT, 'invalid authoritative observation'); END"
            )
        )
        op.execute(
            sa.text(
                f"CREATE TRIGGER booking_action_verified_write_back_{suffix} "
                f"BEFORE {operation} ON booking_action WHEN {booking_condition} "
                "BEGIN SELECT RAISE(ABORT, 'invalid booking write-back state'); END"
            )
        )


def _drop_sqlite_guards() -> None:
    for name in (
        "availability_slot_authoritative_observation_insert",
        "availability_slot_authoritative_observation_update",
        "booking_action_verified_write_back_insert",
        "booking_action_verified_write_back_update",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
