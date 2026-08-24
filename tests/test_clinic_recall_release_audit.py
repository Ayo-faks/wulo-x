from __future__ import annotations

from pathlib import Path

import pytest
from src.clinic_recall.release_audit import (
    AuditRoleError,
    AuditRoleEvidence,
    _audit_password,
    require_ordinary_audit_role,
    require_select_only_table_access,
)
from src.clinic_recall.release_migration import (
    MigrationExecutionError,
    _bootstrap_runtime_configuration,
)


def test_audit_role_requires_all_elevated_flags_false() -> None:
    ordinary = AuditRoleEvidence(
        superuser=False,
        bypass_rls=False,
        create_db=False,
        create_role=False,
        replication=False,
        role_memberships=0,
    )
    require_ordinary_audit_role(ordinary)

    for field in (
        "superuser",
        "bypass_rls",
        "create_db",
        "create_role",
        "replication",
        "role_memberships",
    ):
        elevated = AuditRoleEvidence(
            superuser=field == "superuser",
            bypass_rls=field == "bypass_rls",
            create_db=field == "create_db",
            create_role=field == "create_role",
            replication=field == "replication",
            role_memberships=1 if field == "role_memberships" else 0,
        )
        with pytest.raises(AuditRoleError):
            require_ordinary_audit_role(elevated)


def test_audit_bootstrap_rejects_mixed_database_credential_sources(monkeypatch) -> None:
    monkeypatch.setenv(
        "CLINIC_RECALL_DATABASE_URL",
        "postgresql+psycopg://admin:test@postgres.example.test/clinic_recall",
    )
    monkeypatch.delenv("CLINIC_RECALL_AUDIT_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "AZURE_APPCONFIG_ENDPOINT",
        "https://config.example.test.azconfig.io",
    )

    with pytest.raises(MigrationExecutionError, match="one explicit target"):
        _bootstrap_runtime_configuration(require_audit=True)


def test_audit_credential_must_match_selected_database_target(monkeypatch) -> None:
    monkeypatch.setenv(
        "CLINIC_RECALL_AUDIT_DATABASE_URL",
        "host=postgres.example.test dbname=clinic_recall "
        "user=clinic_recall_audit password=tests-only sslmode=require",
    )
    assert (
        _audit_password(
            expected_host="POSTGRES.EXAMPLE.TEST.",
            expected_database="clinic_recall",
        )
        == "tests-only"
    )

    with pytest.raises(AuditRoleError, match="target mismatch"):
        _audit_password(
            expected_host="postgres.staging.example.test",
            expected_database="clinic_recall",
        )


def test_audit_table_verification_requires_select_without_mutation() -> None:
    class _Result:
        def __init__(self, row):
            self._row = row

        def one(self):
            return self._row

    class _Connection:
        def __init__(self, row):
            self._row = row

        def execute(self, _statement, _parameters):
            return _Result(self._row)

    require_select_only_table_access(_Connection((True, False, False, False, False, False, False)))

    for row in (
        (False, False, False, False, False, False, False),
        (True, True, False, False, False, False, False),
        (True, False, False, False, False, False, True),
    ):
        with pytest.raises(AuditRoleError, match="SELECT-only"):
            require_select_only_table_access(_Connection(row))


def test_audit_role_is_always_provisioned_and_never_granted_dml() -> None:
    terraform = Path("infra/terraform/clinic-recall-audit.tf").read_text(encoding="utf-8")
    runner = Path("src/clinic_recall/release_audit.py").read_text(encoding="utf-8")

    assert 'default     = true' in terraform
    assert 'name         = "clinic-recall-audit-db-password"' in terraform
    assert 'name = "clinic-recall-audit-db-connection-string"' in terraform
    assert "user=clinic_recall_audit" in terraform
    for required in (
        "NOSUPERUSER",
        "NOBYPASSRLS",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "pg_auth_members",
        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES",
        "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS",
        "ALTER DEFAULT PRIVILEGES",
    ):
        assert required in runner
    assert "GRANT SELECT" in runner
    for forbidden in ("GRANT INSERT", "GRANT UPDATE", "GRANT DELETE"):
        assert forbidden not in runner


def test_audit_connection_is_key_vault_backed_and_postdeploy_ordered() -> None:
    provider = Path("apps/artagent/backend/config/appconfig_provider.py").read_text(
        encoding="utf-8"
    )
    sync = Path("devops/scripts/azd/helpers/sync-appconfig.sh").read_text(encoding="utf-8")
    postdeploy = Path("devops/scripts/azd/postdeploy.sh").read_text(encoding="utf-8")

    assert (
        '"app/postgres/audit-connection-string": '
        '"CLINIC_RECALL_AUDIT_DATABASE_URL"'
    ) in provider
    assert (
        'set_kv_ref "app/postgres/audit-connection-string" '
        '"clinic-recall-audit-db-connection-string"'
    ) in sync
    assert "audit_db_configuration_failed=true" in sync
    assert (
        '"$cliniko_configuration_failed" == "true" || '
        '"$audit_db_configuration_failed" == "true"'
    ) in sync
    assert "CLINIC_RECALL_AUDIT_ROLE_AUTHORIZATION" in postdeploy
    assert postdeploy.count("--authorization '$authorization'") == 2
    assert postdeploy.count("--expected-host '$postgres_host'") == 2
    assert postdeploy.count("--expected-database '$postgres_database'") == 2
    migration_call = postdeploy.index("task_migrate_clinic_recall || exit 1")
    audit_call = postdeploy.index("task_configure_clinic_recall_audit_role || exit 1")
    webhook_call = postdeploy.index("preflight_endpoint", audit_call)
    assert migration_call < audit_call < webhook_call
