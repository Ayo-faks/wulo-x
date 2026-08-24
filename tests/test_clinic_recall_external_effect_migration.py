"""Migration and schema contracts for the PR-01 external-effect outbox."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from src.clinic_recall import telemetry
from src.clinic_recall.models import TENANT_TABLES, ExternalEffect

MIGRATION_PATH = Path(
    "infra/postgres/migrations/versions/0014_external_effect_outbox.py"
)


def test_external_effect_schema_is_minimized_and_rls_registered() -> None:
    assert "external_effect" in TENANT_TABLES
    columns = set(ExternalEffect.__table__.c.keys())
    assert columns.isdisjoint(
        {
            "name",
            "phone",
            "email",
            "message_body",
            "transcript",
            "raw_callback",
        }
    )
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in ExternalEffect.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert ("clinic_id", "effect_type", "idempotency_key") in unique_columns

    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "0014_external_effect_outbox"' in migration
    assert 'down_revision: str | None = "0013_recall_task_idempotency"' in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "external_effect_tenant_isolation" in migration


def test_sqlite_migration_0014_round_trip(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "external-effect-migration.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    alembic_config = Config("infra/postgres/alembic.ini")
    telemetry.logger.disabled = False

    command.upgrade(alembic_config, "0014_external_effect_outbox")
    assert telemetry.logger.disabled is False
    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    assert "external_effect" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("external_effect")} == set(
        ExternalEffect.__table__.c.keys()
    )
    assert {
        index["name"] for index in inspector.get_indexes("external_effect")
    } >= {
        "ix_external_effect_claim",
        "ix_external_effect_clinic_id",
        "ix_external_effect_expired_lease",
    }

    command.downgrade(alembic_config, "0013_recall_task_idempotency")
    assert "external_effect" not in sa.inspect(engine).get_table_names()

    command.upgrade(alembic_config, "0014_external_effect_outbox")
    assert "external_effect" in sa.inspect(engine).get_table_names()
    engine.dispose()