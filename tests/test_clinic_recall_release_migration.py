from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from src.clinic_recall.release_migration import (
    MigrationAuthorizationError,
    MigrationSnapshot,
    _bootstrap_runtime_configuration,
    require_expected_target,
    upgrade_required,
)


def _snapshot(*, current_head: str | None = "0014_external_effect_outbox") -> MigrationSnapshot:
    return MigrationSnapshot(
        current_head=current_head,
        source_head="0024_receipted_handoffs",
        source_sha="a" * 40,
        database_host="postgres.phase0.example.test",
        database_name="clinic_recall",
        role_superuser=False,
        role_bypass_rls=True,
        role_create_db=True,
        role_create_role=True,
    )


def test_release_migration_noops_at_exact_head_without_authorization() -> None:
    assert upgrade_required(
        _snapshot(current_head="0024_receipted_handoffs"),
        authorization="",
    ) is False


def test_release_migration_requires_source_identity_at_current_head() -> None:
    snapshot = _snapshot(current_head="0024_receipted_handoffs")

    with pytest.raises(MigrationAuthorizationError, match="source identity"):
        upgrade_required(
            replace(snapshot, source_sha=""),
            authorization="",
        )


def test_release_migration_requires_exact_source_and_head_authorization() -> None:
    snapshot = _snapshot()

    for authorization in ("", "a" * 40, f"{'b' * 40}:0024_receipted_handoffs"):
        with pytest.raises(MigrationAuthorizationError):
            upgrade_required(snapshot, authorization=authorization)

    assert upgrade_required(
        snapshot,
        authorization=f"{'a' * 40}:0024_receipted_handoffs",
    ) is True


def test_release_migration_rejects_missing_current_head() -> None:
    with pytest.raises(MigrationAuthorizationError):
        upgrade_required(
            _snapshot(current_head=None),
            authorization=f"{'a' * 40}:0024_receipted_handoffs",
        )


def test_release_migration_requires_exact_database_target() -> None:
    snapshot = _snapshot()
    require_expected_target(
        snapshot,
        expected_host="POSTGRES.PHASE0.EXAMPLE.TEST.",
        expected_database="clinic_recall",
    )

    for host, database in (
        ("postgres.staging.example.test", "clinic_recall"),
        ("postgres.phase0.example.test", "clinic_recall_staging"),
        ("", "clinic_recall"),
        ("postgres.phase0.example.test", ""),
    ):
        with pytest.raises(MigrationAuthorizationError):
            require_expected_target(
                snapshot,
                expected_host=host,
                expected_database=database,
            )


def test_explicit_database_url_cannot_be_overridden_by_appconfig(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        "postgresql+psycopg://test:only@127.0.0.1:5432/test",
    )
    monkeypatch.setenv(
        "AZURE_APPCONFIG_ENDPOINT",
        "https://config.example.test.azconfig.io",
    )
    monkeypatch.setattr(
        "apps.artagent.backend.config.appconfig_provider.bootstrap_appconfig",
        lambda: calls.append(True) or True,
    )

    _bootstrap_runtime_configuration()

    assert calls == []


def test_backend_image_and_postdeploy_own_release_migration() -> None:
    dockerfile = Path("apps/artagent/backend/Dockerfile").read_text(encoding="utf-8")
    dockerignore = Path("apps/artagent/backend/Dockerfile.dockerignore").read_text(
        encoding="utf-8"
    )
    postdeploy = Path("devops/scripts/azd/postdeploy.sh").read_text(encoding="utf-8")

    assert "COPY ./infra/postgres /app/infra/postgres" in dockerfile
    assert "!infra/postgres/**" in dockerignore
    assert "task_migrate_clinic_recall" in postdeploy
    assert "CLINIC_RECALL_MIGRATION_AUTHORIZATION" in postdeploy
    assert "--authorization '$authorization'" in postdeploy
    assert "--expected-host '$postgres_host'" in postdeploy
    assert "--expected-database '$postgres_database'" in postdeploy
    assert "azd_get POSTGRES_HOST" in postdeploy
    assert "azd_get POSTGRES_DATABASE_NAME" in postdeploy
    assert postdeploy.index("task_migrate_clinic_recall") < postdeploy.index(
        "upsert_event_subscription"
    )
