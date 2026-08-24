"""Migration contracts for PR-11 identity evidence tiers."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from src.clinic_recall.models import (
    TENANT_TABLES,
    BookingAction,
    IdentityEvidence,
    IdentityFactorAttempt,
)

MIGRATION_PATH = Path(
    "infra/postgres/migrations/versions/0023_identity_evidence_tiers.py"
)


def _config(database_url: str, monkeypatch) -> Config:
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    return Config("infra/postgres/alembic.ini")


def test_models_register_minimized_identity_tables_for_rls() -> None:
    assert {"identity_evidence", "identity_factor_attempt"} <= set(TENANT_TABLES)
    evidence_columns = set(IdentityEvidence.__table__.c.keys())
    attempt_columns = set(IdentityFactorAttempt.__table__.c.keys())
    assert {
        "session_key_hash",
        "route_key_hash",
        "patient_key_hash",
        "policy_version",
        "tier",
        "expires_at",
    } <= evidence_columns
    assert {"factor_type", "result", "attempted_at"} <= attempt_columns
    forbidden = {
        "answer",
        "raw_answer",
        "factor_value",
        "name",
        "date_of_birth",
        "phone",
        "payload",
    }
    assert forbidden.isdisjoint(evidence_columns | attempt_columns)
    assert {
        "identity_evidence_id",
        "identity_policy_version",
        "identity_evidence_revision",
    } <= set(BookingAction.__table__.c.keys())


def test_0023_declares_linear_head_forced_rls_and_guarded_downgrade() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "0023_identity_evidence_tiers"' in migration
    assert 'down_revision: str | None = "0022_cliniko_booking_effect"' in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "revoke identity evidence before downgrade" in migration
    assert "sa.JSON" not in migration


def test_sqlite_zero_to_0023_matches_models_and_round_trips(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr11-zero.db'}"
    config = _config(database_url, monkeypatch)

    command.upgrade(config, "0023_identity_evidence_tiers")
    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    assert {column["name"] for column in inspector.get_columns("identity_evidence")} == set(
        IdentityEvidence.__table__.c.keys()
    )
    assert {
        column["name"] for column in inspector.get_columns("identity_factor_attempt")
    } == set(IdentityFactorAttempt.__table__.c.keys())
    assert {column["name"] for column in inspector.get_columns("booking_action")} == set(
        BookingAction.__table__.c.keys()
    )
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0023_identity_evidence_tiers"
        )

    command.downgrade(config, "0022_cliniko_booking_effect")
    assert "identity_evidence" not in sa.inspect(engine).get_table_names()
    assert "identity_factor_attempt" not in sa.inspect(engine).get_table_names()
    command.upgrade(config, "0023_identity_evidence_tiers")
    assert "identity_evidence" in sa.inspect(engine).get_table_names()
    engine.dispose()


def test_0023_downgrade_refuses_retained_evidence(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr11-unsafe.db'}"
    config = _config(database_url, monkeypatch)
    command.upgrade(config, "0023_identity_evidence_tiers")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO clinic (id, name) VALUES ('clinic-pr11', 'PR11')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO identity_evidence ("
                "id, clinic_id, session_key_hash, route_key_hash, patient_key_hash, "
                "channel, policy_version, tier, state, reason, matched_factor_count, "
                "dob_verified, attempt_count, max_attempts, issued_at, expires_at, "
                "revision"
                ") VALUES ("
                "'evidence-pr11', 'clinic-pr11', :session_hash, :route_hash, "
                ":patient_hash, 'sms', 'synthetic-v1', 't0', 'active', "
                "'route_only', 0, false, 0, 3, :issued_at, :expires_at, 0)"
            ),
            {
                "session_hash": "a" * 64,
                "route_hash": "b" * 64,
                "patient_hash": "c" * 64,
                "issued_at": "2026-07-26T10:00:00+00:00",
                "expires_at": "2026-07-26T10:05:00+00:00",
            },
        )

    with pytest.raises(RuntimeError, match="revoke identity evidence before downgrade"):
        command.downgrade(config, "0022_cliniko_booking_effect")

    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0023_identity_evidence_tiers"
        )
    engine.dispose()