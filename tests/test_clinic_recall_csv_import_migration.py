"""Migration and model contracts for PR-08 controlled CSV import."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from src.clinic_recall.models import (
    TENANT_TABLES,
    ImportBatch,
    ImportMatchReview,
    PatientSourceLink,
    RightsAliasTombstone,
)

MIGRATION_PATH = Path("infra/postgres/migrations/versions/0021_controlled_csv_import.py")

_PR08_TABLES = (
    "import_batch",
    "patient_source_link",
    "import_match_review",
    "rights_alias_tombstone",
)

_MODELS = {
    "import_batch": ImportBatch,
    "patient_source_link": PatientSourceLink,
    "import_match_review": ImportMatchReview,
    "rights_alias_tombstone": RightsAliasTombstone,
}


def test_pr08_models_are_minimized_and_tenant_scoped() -> None:
    assert set(_PR08_TABLES) <= set(TENANT_TABLES)
    # The provenance schema cannot represent raw uploads or row-level PII.
    forbidden = {
        "filename",
        "file_name",
        "path",
        "raw",
        "raw_bytes",
        "content",
        "body",
        "rows",
        "row_errors",
        "name",
        "phone",
        "email",
        "mime_type",
        "content_type",
    }
    for model in _MODELS.values():
        assert forbidden.isdisjoint(model.__table__.c.keys()), model.__tablename__
    assert {c.name for c in ImportBatch.__table__.constraints} >= {
        "uq_import_batch_clinic_id_id",
        "ck_import_batch_file_hash_length",
        "ck_import_batch_summary_hash_length",
        "ck_import_batch_policy_hash_length",
        "ck_import_batch_counts_nonnegative",
        "ck_import_batch_row_counts_exact",
        "ck_import_batch_error_count_bounded",
        "ck_import_batch_completed_evidence",
        "ck_import_batch_timestamp_order",
        "ck_import_batch_lifecycle_order",
        "ck_import_batch_completed_counts_exact",
    }
    assert {c.name for c in PatientSourceLink.__table__.constraints} >= {
        "uq_patient_source_link_clinic_id_id",
        "uq_patient_source_link_provider_ref",
        "fk_patient_source_link_patient_tenant",
        "fk_patient_source_link_batch_tenant",
    }
    assert {c.name for c in ImportMatchReview.__table__.constraints} >= {
        "uq_import_match_review_scope",
        "fk_import_match_review_patient_tenant",
        "fk_import_match_review_batch_tenant",
        "fk_import_match_review_source_link_tenant",
        "ck_import_match_review_resolution_state",
    }
    assert {c.name for c in RightsAliasTombstone.__table__.constraints} >= {
        "uq_rights_alias_tombstone_subject",
        "fk_rights_alias_tombstone_request_tenant",
    }


def test_0021_declares_forced_rls_and_guarded_downgrade() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "0021_controlled_csv_import"' in migration
    assert 'down_revision: str | None = "0020_availability_booking_state"' in migration
    for value in ("csv_import_preview", "csv_import_approve", "csv_import_match"):
        assert f"ADD VALUE IF NOT EXISTS '{value}'" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "roll back by disabling CSV import" in migration


def test_alembic_has_exactly_one_current_head() -> None:
    config = Config("infra/postgres/alembic.ini")
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0024_receipted_handoffs"]


def test_sqlite_0021_round_trip_matches_models(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr08-empty.db'}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    config = Config("infra/postgres/alembic.ini")

    command.upgrade(config, "0021_controlled_csv_import")
    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    assert set(_PR08_TABLES) <= set(inspector.get_table_names())
    for table, model in _MODELS.items():
        assert {column["name"] for column in inspector.get_columns(table)} == set(
            model.__table__.c.keys()
        ), table
    index_names = {index["name"] for index in inspector.get_indexes("import_batch")}
    assert "uq_import_batch_live_file" in index_names
    link_indexes = {index["name"] for index in inspector.get_indexes("patient_source_link")}
    assert "uq_patient_source_link_active" in link_indexes

    command.downgrade(config, "0020_availability_booking_state")
    assert set(_PR08_TABLES).isdisjoint(sa.inspect(engine).get_table_names())
    command.upgrade(config, "0021_controlled_csv_import")  # replay after downgrade
    assert set(_PR08_TABLES) <= set(sa.inspect(engine).get_table_names())
    engine.dispose()


def test_sqlite_0021_downgrade_refuses_retained_state(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr08-forward-only.db'}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, "0021_controlled_csv_import")
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO clinic (id, name) VALUES ('clinic-pr08', 'PR08 Clinic')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO import_batch ("
                "id, clinic_id, state, file_sha256, validation_summary_sha256, "
                "schema_version, source_system, export_at, preview_requested_at, "
                "preview_actor, preview_expires_at, preview_upload_disposed_at"
                ") VALUES ("
                "'impb-migration', 'clinic-pr08', 'preview_valid', :digest, :digest, "
                "'wulo-csv-v1', 'csv', :now, :now, 'staff:test', :later, :now"
                ")"
            ),
            {
                "digest": "a" * 64,
                "now": "2026-07-26T00:00:00+00:00",
                "later": "2026-07-26T01:00:00+00:00",
            },
        )

    with pytest.raises(RuntimeError, match="roll back by disabling CSV import"):
        command.downgrade(config, "0020_availability_booking_state")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0021_controlled_csv_import"
        )
    engine.dispose()


def test_sqlite_0021_live_file_uniqueness_allows_superseded(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'pr08-unique.db'}"
    monkeypatch.setenv("CLINIC_RECALL_DATABASE_URL", database_url)
    config = Config("infra/postgres/alembic.ini")
    command.upgrade(config, "0021_controlled_csv_import")
    engine = sa.create_engine(database_url)

    def insert(connection, batch_id: str, state: str):
        connection.execute(
            sa.text(
                "INSERT INTO import_batch ("
                "id, clinic_id, state, file_sha256, validation_summary_sha256, "
                "schema_version, source_system, export_at, preview_requested_at, "
                "preview_actor, preview_expires_at, preview_upload_disposed_at"
                ") VALUES ("
                f"'{batch_id}', 'clinic-pr08', '{state}', :digest, :digest, "
                "'wulo-csv-v1', 'csv', :now, :now, 'staff:test', :later, :now"
                ")"
            ),
            {
                "digest": "b" * 64,
                "now": "2026-07-26T00:00:00+00:00",
                "later": "2026-07-26T01:00:00+00:00",
            },
        )

    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO clinic (id, name) VALUES ('clinic-pr08', 'PR08 Clinic')")
        )
        insert(connection, "impb-live-1", "preview_valid")
        insert(connection, "impb-old-1", "superseded")  # allowed outside the index
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            insert(connection, "impb-live-2", "preview_valid")
    engine.dispose()
