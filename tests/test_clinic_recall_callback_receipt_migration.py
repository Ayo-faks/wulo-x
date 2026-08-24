"""Migration and schema contracts for the PR-02 callback receipt inbox."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from src.clinic_recall.durable.callbacks import effect_token_scope_id, parse_effect_token
from src.clinic_recall.models import (
    TENANT_TABLES,
    ExternalEffect,
    ProviderCallbackReceipt,
)

MIGRATION_PATH = Path("infra/postgres/migrations/versions/0015_provider_callback_receipts.py")

RECEIPT_COLUMNS = {
    "id",
    "clinic_id",
    "external_effect_id",
    "provider",
    "callback_kind",
    "deduplication_hash",
    "effect_token_hash",
    "provider_resource_id",
    "normalized_status",
    "provider_sequence",
    "provider_observed_at",
    "payload_hash",
    "state",
    "reason_code",
    "processing_attempts",
    "lease_owner",
    "lease_expires_at",
    "received_at",
    "applied_at",
    "created_at",
    "updated_at",
}


def test_callback_receipt_schema_is_minimized_and_rls_registered() -> None:
    assert "provider_callback_receipt" in TENANT_TABLES
    assert set(ProviderCallbackReceipt.__table__.c.keys()) == RECEIPT_COLUMNS
    assert {"callback_token", "provider_sequence"} <= set(ExternalEffect.__table__.c.keys())
    assert RECEIPT_COLUMNS.isdisjoint(
        {
            "name",
            "phone",
            "email",
            "message_body",
            "raw_body",
            "raw_callback",
            "recording_url",
            "transcript",
            "signature",
            "provider_error",
        }
    )
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in ProviderCallbackReceipt.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert (
        "clinic_id",
        "provider",
        "callback_kind",
        "deduplication_hash",
    ) in unique_columns

    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "0015_provider_callback_receipts"' in migration
    assert 'down_revision: str | None = "0014_external_effect_outbox"' in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "provider_callback_receipt_tenant_isolation" in migration


def test_sqlite_migration_upgrades_0014_backfills_token_and_round_trips(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "provider-callback-receipt.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    alembic_config = Config("infra/postgres/alembic.ini")

    command.upgrade(alembic_config, "0013_recall_task_idempotency")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE IF EXISTS provider_callback_receipt"))
        connection.execute(sa.text("DROP TABLE IF EXISTS external_effect"))
    command.upgrade(alembic_config, "0014_external_effect_outbox")
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO clinic (id, name) VALUES ('clinic-existing', 'Existing')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO external_effect "
                "(id, clinic_id, aggregate_type, aggregate_id, effect_type, "
                "idempotency_key, payload_version, payload, request_hash, state, "
                "available_at, attempt_count, max_attempts) VALUES "
                "('effect-existing', 'clinic-existing', 'outreach_job', 'job-existing', "
                "'sms', 'recall-sms:existing', 1, '{}', :request_hash, 'reconcile_required', "
                "CURRENT_TIMESTAMP, 1, 1)"
            ),
            {"request_hash": "0" * 64},
        )

    command.upgrade(alembic_config, "0015_provider_callback_receipts")
    inspector = sa.inspect(engine)
    assert "provider_callback_receipt" in inspector.get_table_names()
    assert {
        column["name"] for column in inspector.get_columns("provider_callback_receipt")
    } == RECEIPT_COLUMNS
    assert {column["name"] for column in inspector.get_columns("external_effect")} >= {
        "callback_token",
        "provider_sequence",
    }
    assert {index["name"] for index in inspector.get_indexes("provider_callback_receipt")} >= {
        "ix_provider_callback_receipt_claim",
        "ix_provider_callback_receipt_effect",
        "ix_provider_callback_receipt_expired_lease",
    }
    with engine.connect() as connection:
        token = connection.execute(
            sa.text("SELECT callback_token FROM external_effect WHERE id = 'effect-existing'")
        ).scalar_one()
        revision = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert parse_effect_token(token).scope_id == effect_token_scope_id("clinic-existing")
    assert revision == "0015_provider_callback_receipts"

    command.downgrade(alembic_config, "0014_external_effect_outbox")
    inspector = sa.inspect(engine)
    assert "provider_callback_receipt" not in inspector.get_table_names()
    assert "callback_token" not in {
        column["name"] for column in inspector.get_columns("external_effect")
    }
    engine.dispose()
