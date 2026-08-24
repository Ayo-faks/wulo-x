"""Migration contracts for PR-13 programme and cumulative cohort controls."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from src.clinic_recall.models import TENANT_TABLES, PilotParticipant, PilotProgramme

MIGRATION_PATH = Path("infra/postgres/migrations/versions/0017_pilot_programme_controls.py")

PROGRAMME_COLUMNS = {
    "id",
    "clinic_id",
    "environment",
    "release_identity",
    "state",
    "maximum_unique_patients",
    "active_cumulative_limit",
    "released_at",
    "released_by",
    "release_evidence_hash",
    "paused_at",
    "paused_by",
    "pause_reason",
    "created_at",
    "updated_at",
}
PARTICIPANT_COLUMNS = {
    "id",
    "clinic_id",
    "pilot_programme_id",
    "patient_id",
    "patient_key_hash",
    "ordinal",
    "wave",
    "enrolled_at",
    "released_at",
    "first_contact_at",
    "created_at",
    "updated_at",
}


def test_pr13_tables_are_minimized_forced_rls_and_immutable() -> None:
    assert {"pilot_programme", "pilot_participant"} <= set(TENANT_TABLES)
    assert set(PilotProgramme.__table__.c.keys()) == PROGRAMME_COLUMNS
    assert set(PilotParticipant.__table__.c.keys()) == PARTICIPANT_COLUMNS
    assert PilotParticipant.__table__.c.patient_id.nullable is True

    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "0017_pilot_programme_controls"' in migration
    assert 'down_revision: str | None = "0016_scheduled_cadence"' in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "pilot_programme_tenant_isolation" in migration
    assert "pilot_participant_tenant_isolation" in migration
    assert "protect_pilot_participant_identity" in migration
    assert "protect_pilot_programme" in migration
    assert "pilot_programme_transition_guard" in migration
    assert 'ondelete="RESTRICT"' in migration
    assert "fk_pilot_participant_patient_tenant" in migration
    assert "uq_patient_clinic_id_id" in migration
    assert "anonymize_pilot_participant_on_patient_delete" in migration
    assert "pilot_participant_patient_erasure" in migration
    assert "patient_key_hash" in migration
    assert "uq_pilot_participant_patient_reference" in migration


def test_postgres_trigger_installation_is_reentrant_and_downgrade_complete() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    installer = migration.split("def _install_invariant_triggers() -> None:", 1)[1]
    trigger_tables = {
        "pilot_programme_transition_guard": "pilot_programme",
        "pilot_participant_insert_guard": "pilot_participant",
        "pilot_participant_identity_guard": "pilot_participant",
        "pilot_participant_patient_erasure": "patient",
    }

    for trigger_name, table_name in trigger_tables.items():
        drop = f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"
        create = f"CREATE TRIGGER {trigger_name}"
        assert drop in installer
        assert installer.index(drop) < installer.index(create)

    downgrade = migration.split("def downgrade() -> None:", 1)[1].split(
        "def _create_pilot_programme() -> None:", 1
    )[0]
    assert "DROP TRIGGER IF EXISTS pilot_programme_transition_guard ON pilot_programme" in downgrade


def test_sqlite_migration_upgrades_0016_and_round_trips(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "pilot-controls.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    alembic_config = Config("infra/postgres/alembic.ini")

    command.upgrade(alembic_config, "0016_scheduled_cadence")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE IF EXISTS pilot_participant"))
        connection.execute(sa.text("DROP TABLE IF EXISTS pilot_programme"))
    assert "pilot_programme" not in sa.inspect(engine).get_table_names()

    command.upgrade(alembic_config, "0017_pilot_programme_controls")
    inspector = sa.inspect(engine)
    assert {"pilot_programme", "pilot_participant"} <= set(inspector.get_table_names())
    assert {column["name"] for column in inspector.get_columns("pilot_programme")} == (
        PROGRAMME_COLUMNS
    )
    assert {
        column["name"] for column in inspector.get_columns("pilot_participant")
    } == PARTICIPANT_COLUMNS
    with engine.connect() as connection:
        revision = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert revision == "0017_pilot_programme_controls"

    command.downgrade(alembic_config, "0016_scheduled_cadence")
    after = set(sa.inspect(engine).get_table_names())
    assert "pilot_programme" not in after
    assert "pilot_participant" not in after
    assert "cadence_cursor" in after
    engine.dispose()
