"""Migration contracts for the PR-09 all-call consent ledger."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from src.clinic_recall.models import CallRecord, InboundCall

MIGRATION_PATH = Path("infra/postgres/migrations/versions/0018_recording_consent_ledger.py")

NEW_CALL_RECORD_COLUMNS = {
    "external_effect_id",
    "inbound_call_id",
    "consent_state",
    "consent_asked_at",
    "consent_decided_at",
    "consent_decision_source",
    "consent_version",
    "recording_requested_at",
    "recording_started_at",
    "recording_stop_requested_at",
    "recording_stopped_at",
    "deletion_state",
}


def test_pr09_model_has_all_call_anchors_and_minimized_consent_fields() -> None:
    columns = set(CallRecord.__table__.c.keys())
    assert NEW_CALL_RECORD_COLUMNS <= columns
    assert CallRecord.__table__.c.provider_call_id.nullable is True
    assert {constraint.name for constraint in CallRecord.__table__.constraints} >= {
        "ck_call_record_has_trusted_anchor",
        "ck_call_record_not_both_internal_anchors",
        "fk_call_record_external_effect_tenant",
        "fk_call_record_inbound_call_tenant",
        "fk_call_record_inbound_provider_tenant",
        "fk_call_record_patient_tenant",
        "uq_call_record_external_effect",
        "uq_call_record_inbound_call",
    }
    assert {constraint.name for constraint in InboundCall.__table__.constraints} >= {
        "uq_inbound_call_clinic_id_id",
        "uq_inbound_call_clinic_id_provider",
    }


def test_pr09_migration_is_additive_forced_rls_and_truthful_about_legacy_rows() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "0018_recording_consent_ledger"' in migration
    assert 'down_revision: str | None = "0017_pilot_programme_controls"' in migration
    assert "recording_consent_state" in migration
    assert "recording_consent_source" in migration
    assert "recording_deletion_state" in migration
    assert "legacy-stored-consent-v0" in migration
    assert "ambiguous" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "fk_call_record_external_effect_tenant" in migration
    assert "fk_call_record_inbound_call_tenant" in migration
    assert "fk_call_record_inbound_provider_tenant" in migration
    assert "fk_call_record_patient_tenant" in migration
    assert "call_record_inbound_identity_guard" in migration
    assert "ck_call_record_has_trusted_anchor" in migration
    assert "ck_call_record_not_both_internal_anchors" in migration


def test_sqlite_migration_upgrades_0017_and_round_trips(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "recording-consent.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    config = Config("infra/postgres/alembic.ini")

    command.upgrade(config, "0017_pilot_programme_controls")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO clinic (id, name) VALUES "
                "('clinic-legacy-recording', 'Legacy Recording Clinic')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO call_record ("
                "id, clinic_id, provider, provider_call_id, direction, "
                "recording_status, consent_snapshot"
                ") VALUES ("
                "'callrec-legacy', 'clinic-legacy-recording', 'twilio', "
                "'CAlegacy', 'outbound', 'stored', '{\"record_call\": true}'"
                ")"
            )
        )

    command.upgrade(config, "0018_recording_consent_ledger")

    inspector = sa.inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("call_record")}
    assert NEW_CALL_RECORD_COLUMNS <= set(columns)
    assert columns["provider_call_id"]["nullable"] is True
    with engine.connect() as connection:
        legacy = connection.execute(
            sa.text(
                "SELECT consent_state, consent_decision_source, consent_version, "
                "deletion_state FROM call_record WHERE id = 'callrec-legacy'"
            )
        ).one()
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert tuple(legacy) == (
        "ambiguous",
        "policy",
        "legacy-stored-consent-v0",
        "not_requested",
    )
    assert revision == "0018_recording_consent_ledger"

    command.downgrade(config, "0017_pilot_programme_controls")
    after = {column["name"] for column in sa.inspect(engine).get_columns("call_record")}
    assert NEW_CALL_RECORD_COLUMNS.isdisjoint(after)
    assert "recording_status" in after
    engine.dispose()


def test_sqlite_downgrade_refuses_pr09_call_rows(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'recording-consent-forward-only.db'}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, "0018_recording_consent_ledger")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO clinic (id, name) VALUES ('clinic-pr09', 'PR-09 Clinic')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO inbound_call ("
                "id, clinic_id, provider, provider_call_id, called_number, status"
                ") VALUES ("
                "'inbound-pr09', 'clinic-pr09', 'twilio', 'CApr09', '+44111', 'started'"
                ")"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO call_record ("
                "id, clinic_id, inbound_call_id, provider, provider_call_id, direction, "
                "consent_state, recording_status, deletion_state"
                ") VALUES ("
                "'callrec-pr09', 'clinic-pr09', 'inbound-pr09', 'twilio', 'CApr09', "
                "'inbound', 'not_asked', 'none', 'not_requested'"
                ")"
            )
        )

    with pytest.raises(RuntimeError, match="rollback by disabling recording"):
        command.downgrade(config, "0017_pilot_programme_controls")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0018_recording_consent_ledger"
        )
    engine.dispose()
