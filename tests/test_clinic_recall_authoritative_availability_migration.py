"""Migration contracts for PR-06 authoritative availability and booking state."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from src.clinic_recall.enums import BookingWriteBackState
from src.clinic_recall.models import AvailabilitySlot, BookingAction

MIGRATION_PATH = Path(
    "infra/postgres/migrations/versions/0020_authoritative_availability_booking_state.py"
)
AVAILABILITY_COLUMNS = {
    "source_provider",
    "business_id",
    "appointment_type_id",
    "fetched_at",
    "expires_at",
}
BOOKING_COLUMNS = {
    "write_back_state",
    "external_appointment_ref",
    "request_hash",
    "provider_attempted_at",
    "read_back_verified_at",
    "conflict_reason",
}


def _config(database_url: str, monkeypatch) -> Config:
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    return Config("infra/postgres/alembic.ini")


def _seed_legacy_rows(engine, *, written_back: bool = False) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO clinic (id, name) VALUES ('clinic-pr06', 'PR06 Clinic')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO patient ("
                "id, clinic_id, source_ref, name, consent_flags, opt_out_flags"
                ") VALUES ("
                "'patient-pr06', 'clinic-pr06', 'P-pr06', 'Synthetic PR06', '{}', '{}'"
                ")"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO appointment ("
                "id, clinic_id, patient_id, source_ref, status, start_at"
                ") VALUES ("
                "'appointment-pr06', 'clinic-pr06', 'patient-pr06', 'A-pr06', "
                "'scheduled', '2026-08-01T09:00:00+00:00'"
                ")"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO availability_slot ("
                "id, clinic_id, source_ref, clinician_id, start_at, end_at, details"
                ") VALUES ("
                "'slot-pr06-legacy', 'clinic-pr06', 'legacy-source', 'legacy-clinician', "
                "'2026-08-01T09:00:00+00:00', '2026-08-01T09:30:00+00:00', '{}'"
                ")"
            )
        )
        if written_back and connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        try:
            connection.execute(
                sa.text(
                    "INSERT INTO booking_action ("
                    "id, clinic_id, appointment_id, type, status, written_back"
                    ") VALUES ("
                    "'booking-pr06-legacy', 'clinic-pr06', 'appointment-pr06', "
                    "'book', 'completed', :written_back"
                    ")"
                ),
                {"written_back": written_back},
            )
        finally:
            if written_back and connection.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")


def test_models_declare_additive_authority_and_write_back_contract() -> None:
    assert AVAILABILITY_COLUMNS <= set(AvailabilitySlot.__table__.c.keys())
    assert BOOKING_COLUMNS <= set(BookingAction.__table__.c.keys())
    availability_constraint_names = {
        constraint.name for constraint in AvailabilitySlot.__table__.constraints
    }
    availability_index_names = {
        index.name for index in AvailabilitySlot.__table__.indexes
    }
    assert (
        "ck_availability_slot_authoritative_observation"
        in availability_constraint_names
    )
    assert "ix_availability_slot_clinic_fresh" in availability_index_names
    assert BookingAction.__table__.c.written_back.nullable is False
    assert BookingAction.__table__.c.write_back_state.nullable is False
    assert set(BookingWriteBackState) == {
        BookingWriteBackState.NOT_ATTEMPTED,
        BookingWriteBackState.PENDING,
        BookingWriteBackState.DISPATCHING,
        BookingWriteBackState.VERIFIED,
        BookingWriteBackState.REJECTED,
        BookingWriteBackState.RECONCILE_REQUIRED,
        BookingWriteBackState.CONFLICT,
    }
    constraint_names = {
        constraint.name for constraint in BookingAction.__table__.constraints
    }
    assert "ck_booking_action_verified_write_back" in constraint_names
    assert "ck_booking_action_request_hash_length" in constraint_names


def test_0020_declares_exact_head_guards_and_no_provider_write() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "0020_availability_booking_state"' in migration
    assert 'down_revision: str | None = "0019_rights_retention_purge"' in migration
    assert "booking_write_back_state" in migration
    assert "ck_availability_slot_authoritative_observation" in migration
    assert "ck_booking_action_verified_write_back" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "POST" not in migration
    assert "PATCH" not in migration
    assert "written_back=True" not in migration


def test_sqlite_zero_to_0020_matches_model_columns(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr06-zero.db'}"
    config = _config(database_url, monkeypatch)

    command.upgrade(config, "0020_availability_booking_state")
    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)

    assert {column["name"] for column in inspector.get_columns("availability_slot")} == set(
        AvailabilitySlot.__table__.c.keys()
    )
    assert {column["name"] for column in inspector.get_columns("booking_action")} == set(
        BookingAction.__table__.c.keys()
    )
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0020_availability_booking_state"
        )
    engine.dispose()


def test_sqlite_0019_upgrade_legacy_defaults_guards_replay_and_round_trip(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr06-upgrade.db'}"
    config = _config(database_url, monkeypatch)
    command.upgrade(config, "0019_rights_retention_purge")
    engine = sa.create_engine(database_url)
    _seed_legacy_rows(engine)

    command.upgrade(config, "0020_availability_booking_state")
    with engine.connect() as connection:
        legacy_slot = connection.execute(
            sa.text(
                "SELECT source_provider, business_id, appointment_type_id, "
                "fetched_at, expires_at FROM availability_slot "
                "WHERE id = 'slot-pr06-legacy'"
            )
        ).one()
        legacy_booking = connection.execute(
            sa.text(
                "SELECT write_back_state, written_back, external_appointment_ref, "
                "request_hash, provider_attempted_at, read_back_verified_at, "
                "conflict_reason FROM booking_action "
                "WHERE id = 'booking-pr06-legacy'"
            )
        ).one()
    assert tuple(legacy_slot) == (None, None, None, None, None)
    assert tuple(legacy_booking) == (
        "not_attempted",
        False,
        None,
        None,
        None,
        None,
        None,
    )

    with engine.begin() as connection:
        with pytest.raises(DBAPIError):
            connection.execute(
                sa.text(
                    "UPDATE booking_action SET written_back = true "
                    "WHERE id = 'booking-pr06-legacy'"
                )
            )
    with engine.begin() as connection:
        with pytest.raises(DBAPIError):
            connection.execute(
                sa.text(
                    "UPDATE availability_slot SET source_provider = 'cliniko' "
                    "WHERE id = 'slot-pr06-legacy'"
                )
            )

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE alembic_version SET version_num = "
                "'0019_rights_retention_purge'"
            )
        )
    command.upgrade(config, "0020_availability_booking_state")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0020_availability_booking_state"
        )

    command.downgrade(config, "0019_rights_retention_purge")
    inspector = sa.inspect(engine)
    assert AVAILABILITY_COLUMNS.isdisjoint(
        {column["name"] for column in inspector.get_columns("availability_slot")}
    )
    assert BOOKING_COLUMNS.isdisjoint(
        {column["name"] for column in inspector.get_columns("booking_action")}
    )
    command.upgrade(config, "0020_availability_booking_state")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT write_back_state FROM booking_action")) == (
            "not_attempted"
        )
    engine.dispose()


def test_0020_fails_closed_for_legacy_written_back_true(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr06-legacy-written.db'}"
    config = _config(database_url, monkeypatch)
    command.upgrade(config, "0019_rights_retention_purge")
    engine = sa.create_engine(database_url)
    _seed_legacy_rows(engine, written_back=True)

    with pytest.raises(RuntimeError, match="legacy written_back rows require review"):
        command.upgrade(config, "0020_availability_booking_state")

    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0019_rights_retention_purge"
        )
    engine.dispose()
