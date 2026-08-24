"""Migration contracts for PR-07 durable Cliniko booking effects."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from src.clinic_recall.enums import ExternalEffectType
from src.clinic_recall.models import ExternalEffect

MIGRATION_PATH = Path(
    "infra/postgres/migrations/versions/0022_cliniko_booking_effect.py"
)
READ_COLUMNS = {
    "read_attempt_count",
    "max_read_attempts",
    "settle_deadline_at",
    "preflight_evidence_hash",
}


def _config(database_url: str, monkeypatch) -> Config:
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    return Config("infra/postgres/alembic.ini")


def _migration_module():
    spec = importlib.util.spec_from_file_location("pr07_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_declares_cliniko_effect_and_bounded_read_state() -> None:
    assert ExternalEffectType.CLINIKO_BOOKING.value == "cliniko_booking"
    assert READ_COLUMNS <= set(ExternalEffect.__table__.c.keys())
    constraint_names = {
        constraint.name for constraint in ExternalEffect.__table__.constraints
    }
    assert "ck_external_effect_read_attempt_bounds" in constraint_names
    assert "ck_external_effect_preflight_evidence_hash" in constraint_names


def test_0022_declares_linear_head_enum_and_guarded_downgrade() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "0022_cliniko_booking_effect"' in migration
    assert 'down_revision: str | None = "0021_controlled_csv_import"' in migration
    assert "autocommit_block" in migration
    assert "ADD VALUE IF NOT EXISTS 'cliniko_booking'" in migration
    assert "disable Cliniko booking state before downgrade" in migration


def test_postgres_downgrade_validation_recovers_from_failed_constraint(
    monkeypatch,
) -> None:
    migration = _migration_module()

    class Savepoint:
        rolled_back = False

        def rollback(self) -> None:
            self.rolled_back = True

        def commit(self) -> None:
            raise AssertionError("unsafe validation must not commit")

    class Bind:
        def __init__(self) -> None:
            self.savepoint = Savepoint()

        def begin_nested(self) -> Savepoint:
            return self.savepoint

    bind = Bind()
    calls = 0

    def create_check_constraint(*_args, **_kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sa.exc.DBAPIError(
                "ALTER TABLE",
                {},
                Exception("unsafe hidden row"),
                False,
            )

    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        create_check_constraint,
    )

    with pytest.raises(RuntimeError, match="disable Cliniko booking state"):
        migration._validate_postgres_downgrade_state()

    assert bind.savepoint.rolled_back is True


def test_sqlite_zero_to_0022_matches_model_and_round_trips(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr07-zero.db'}"
    config = _config(database_url, monkeypatch)

    command.upgrade(config, "0022_cliniko_booking_effect")
    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    assert {column["name"] for column in inspector.get_columns("external_effect")} == set(
        ExternalEffect.__table__.c.keys()
    )
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0022_cliniko_booking_effect"
        )

    command.downgrade(config, "0021_controlled_csv_import")
    assert READ_COLUMNS.isdisjoint(
        {column["name"] for column in sa.inspect(engine).get_columns("external_effect")}
    )
    command.upgrade(config, "0022_cliniko_booking_effect")
    assert READ_COLUMNS <= {
        column["name"] for column in sa.inspect(engine).get_columns("external_effect")
    }
    engine.dispose()


def test_sqlite_0022_replay_is_safe(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr07-replay.db'}"
    config = _config(database_url, monkeypatch)
    command.upgrade(config, "0022_cliniko_booking_effect")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE alembic_version SET version_num = "
                "'0021_controlled_csv_import'"
            )
        )

    command.upgrade(config, "0022_cliniko_booking_effect")

    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0022_cliniko_booking_effect"
        )
    engine.dispose()


def test_0022_downgrade_fails_with_cliniko_effect(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr07-unsafe.db'}"
    config = _config(database_url, monkeypatch)
    command.upgrade(config, "0022_cliniko_booking_effect")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO clinic (id, name) VALUES ('clinic-pr07', 'PR07')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO external_effect ("
                "id, clinic_id, aggregate_type, aggregate_id, effect_type, "
                "idempotency_key, callback_token, payload_version, payload, "
                "request_hash, state, available_at, attempt_count, max_attempts"
                ") VALUES ("
                "'effect-pr07', 'clinic-pr07', 'booking_action', 'action-pr07', "
                "'cliniko_booking', 'cliniko-pr07-v1', :token, 1, :payload, "
                ":request_hash, 'pending', :available_at, 0, 2)"
            ),
            {
                "token": "cr2." + "a" * 64 + "." + "b" * 43,
                "payload": '{"intent":"create","booking_action_id":"action-pr07"}',
                "request_hash": "c" * 64,
                "available_at": "2026-07-25T12:00:00+00:00",
            },
        )

    with pytest.raises(RuntimeError, match="disable Cliniko booking state"):
        command.downgrade(config, "0021_controlled_csv_import")

    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0022_cliniko_booking_effect"
        )
    engine.dispose()