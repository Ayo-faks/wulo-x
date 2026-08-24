"""Migration contracts for PR-03 cadence cursor and dead-letter handoff."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from src.clinic_recall.models import (
    TENANT_TABLES,
    CadenceCursor,
    ExternalEffectHandoff,
)

MIGRATION_PATH = Path("infra/postgres/migrations/versions/0016_scheduled_cadence.py")

CURSOR_COLUMNS = {
    "id",
    "clinic_id",
    "planner_name",
    "watermark_at",
    "last_started_at",
    "last_completed_at",
    "last_run_id",
    "created_at",
    "updated_at",
}
HANDOFF_COLUMNS = {
    "id",
    "clinic_id",
    "external_effect_id",
    "status",
    "reason_code",
    "created_at",
    "updated_at",
}


def test_pr03_tables_are_minimized_and_rls_registered() -> None:
    assert {"cadence_cursor", "external_effect_handoff"} <= set(TENANT_TABLES)
    assert set(CadenceCursor.__table__.c.keys()) == CURSOR_COLUMNS
    assert set(ExternalEffectHandoff.__table__.c.keys()) == HANDOFF_COLUMNS
    forbidden = {
        "patient_id",
        "name",
        "phone",
        "email",
        "message_body",
        "transcript",
        "provider_id",
        "provider_error",
        "acknowledged_at",
    }
    assert CURSOR_COLUMNS.isdisjoint(forbidden)
    assert HANDOFF_COLUMNS.isdisjoint(forbidden)

    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "0016_scheduled_cadence"' in migration
    assert 'down_revision: str | None = "0015_provider_callback_receipts"' in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "cadence_cursor_tenant_isolation" in migration
    assert "external_effect_handoff_tenant_isolation" in migration


def test_sqlite_migration_upgrades_0015_and_round_trips(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "scheduled-cadence.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    alembic_config = Config("infra/postgres/alembic.ini")

    command.upgrade(alembic_config, "0015_provider_callback_receipts")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE IF EXISTS external_effect_handoff"))
        connection.execute(sa.text("DROP TABLE IF EXISTS cadence_cursor"))
    before = set(sa.inspect(engine).get_table_names())
    assert "cadence_cursor" not in before
    assert "external_effect_handoff" not in before

    command.upgrade(alembic_config, "0016_scheduled_cadence")
    inspector = sa.inspect(engine)
    assert {"cadence_cursor", "external_effect_handoff"} <= set(inspector.get_table_names())
    assert {column["name"] for column in inspector.get_columns("cadence_cursor")} == (
        CURSOR_COLUMNS
    )
    assert {
        column["name"] for column in inspector.get_columns("external_effect_handoff")
    } == HANDOFF_COLUMNS
    assert {index["name"] for index in inspector.get_indexes("cadence_cursor")} >= {
        "ix_cadence_cursor_clinic_id",
        "ix_cadence_cursor_clinic_watermark",
    }
    assert {index["name"] for index in inspector.get_indexes("external_effect_handoff")} >= {
        "ix_external_effect_handoff_clinic_id",
        "ix_external_effect_handoff_clinic_status",
    }
    with engine.connect() as connection:
        revision = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert revision == "0016_scheduled_cadence"

    command.downgrade(alembic_config, "0015_provider_callback_receipts")
    after = set(sa.inspect(engine).get_table_names())
    assert "cadence_cursor" not in after
    assert "external_effect_handoff" not in after
    assert "provider_callback_receipt" in after
    engine.dispose()
