"""Static safety contracts for PR-10 rights and retention Azure artifacts."""

from __future__ import annotations

from pathlib import Path

JOB_TERRAFORM = Path("infra/terraform/clinic-recall-rights-jobs.tf")
APPCONFIG_PROVIDER = Path("apps/artagent/backend/config/appconfig_provider.py")
APPCONFIG_SYNC = Path("devops/scripts/azd/helpers/sync-appconfig.sh")
PRIVACY_ROLE_HELPER = Path(
    "devops/scripts/azd/helpers/configure-clinic-recall-privacy-db-role.sh"
)
POSTPROVISION = Path("devops/scripts/azd/postprovision.sh")


def _resource_block(source: str, name: str) -> str:
    marker = f'resource "azurerm_container_app_job" "{name}"'
    start = source.index(marker)
    next_resource = source.find('\nresource "', start + len(marker))
    return source[start:] if next_resource == -1 else source[start:next_resource]


def test_rights_job_is_scheduled_finite_and_off_by_default() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    job = _resource_block(source, "clinic_recall_rights")

    assert 'variable "clinic_recall_rights_job_enabled"' in source
    assert 'variable "clinic_recall_rights_dispatch_enabled"' in source
    assert "count = var.clinic_recall_rights_job_enabled ? 1 : 0" in job
    assert source.count("default     = false") == 7
    assert "schedule_trigger_config" in job
    assert "manual_trigger_config" not in job
    assert "event_trigger_config" not in job
    assert "parallelism              = 1" in job
    assert "replica_completion_count = 1" in job
    assert "replica_retry_limit        = 0" in job
    assert '"src.clinic_recall.durable.rights_worker"' in job
    assert '"--mode"' in job and '"both"' in job
    assert 'name  = "CLINIC_RECALL_DURABLE_RIGHTS_ENABLED"' in job
    assert "var.clinic_recall_rights_dispatch_enabled" in job
    assert "azurerm_user_assigned_identity.clinic_recall_rights[0].id" in job
    assert "azurerm_user_assigned_identity.backend.id" not in job
    assert "azurerm_container_registry.main.login_server" in job


def test_retention_job_is_scheduled_finite_and_off_by_default() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    job = _resource_block(source, "clinic_recall_retention")

    assert 'variable "clinic_recall_retention_job_enabled"' in source
    assert 'variable "clinic_recall_retention_scheduler_enabled"' in source
    assert "count = var.clinic_recall_retention_job_enabled ? 1 : 0" in job
    assert "schedule_trigger_config" in job
    assert "manual_trigger_config" not in job
    assert "event_trigger_config" not in job
    assert "parallelism              = 1" in job
    assert "replica_completion_count = 1" in job
    assert "replica_retry_limit        = 0" in job
    assert '"src.clinic_recall.durable.retention_scheduler"' in job
    assert 'name  = "CLINIC_RECALL_RETENTION_SCHEDULER_ENABLED"' in job
    assert "var.clinic_recall_retention_scheduler_enabled" in job
    assert "azurerm_user_assigned_identity.clinic_recall_retention[0].id" in job
    assert "azurerm_user_assigned_identity.backend.id" not in job


def test_privacy_jobs_have_provenance_bounds_and_explicit_dependencies() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")

    for name in ("clinic_recall_rights", "clinic_recall_retention"):
        job = _resource_block(source, name)
        assert "precondition" in job
        assert "SERVICE_VERSION" in job
        assert "GIT_SHA" in job
        assert "clinic-live-test" not in job
        assert "+44" not in job
    rights = _resource_block(source, "clinic_recall_rights")
    assert "azurerm_role_assignment.clinic_recall_rights_acr_pull" in rights
    assert "azurerm_role_assignment.clinic_recall_rights_appconfig_reader" in rights
    assert "azurerm_role_assignment.clinic_recall_rights_keyvault_secrets" in rights
    assert "azurerm_role_assignment.clinic_recall_rights_recordings_owner" in rights
    retention = _resource_block(source, "clinic_recall_retention")
    assert "azurerm_role_assignment.clinic_recall_retention_acr_pull" in retention
    assert "azurerm_role_assignment.clinic_recall_retention_appconfig_reader" in retention
    assert "azurerm_role_assignment.clinic_recall_retention_keyvault_secrets" in retention
    assert "recordings_owner" not in retention
    assert 'role_definition_name = "Storage Blob Data Owner"' in source
    assert "scope                = azurerm_storage_container.call_recordings.id" in source
    assert source.count("count = var.clinic_recall_rights_job_enabled ? 1 : 0") == 6
    assert source.count("count = var.clinic_recall_retention_job_enabled ? 1 : 0") == 5
    assert "clinic_recall_rights_max_target_attempts must be between 1 and 10" in source
    assert "clinic_recall_rights_batch_limit must be between 1 and 50" in source
    assert "clinic_recall_rights_job_timeout_seconds must be between 60 and 900" in source
    assert "clinic_recall_retention_job_timeout_seconds must be between 60 and 900" in source


def test_jobs_never_receive_provider_or_hmac_secrets_directly() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")

    for forbidden in (
        'name  = "TWILIO_AUTH_TOKEN"',
        'name  = "CLINIC_RECALL_RIGHTS_HMAC_KEY"',
        "clinic_recall_rights_hmac_key_secret_value",
    ):
        assert forbidden not in source
    assert source.count('secret {') == 2
    assert source.count('name                = "privacy-postgres"') == 2
    assert "TWILIO_AUTH_TOKEN" not in source
    assert 'output "CLINIC_RECALL_RIGHTS_HMAC_KEY_VERSION"' in source
    assert 'output "CLINIC_RECALL_RIGHTS_HMAC_KEY"' not in source


def test_privacy_jobs_use_verified_ordinary_postgres_role_secret() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    rights = _resource_block(source, "clinic_recall_rights")
    retention = _resource_block(source, "clinic_recall_retention")

    assert 'variable "clinic_recall_privacy_db_role_ready"' in source
    assert 'variable "clinic_recall_privacy_db_role_ready_epoch"' in source
    assert 'default     = false' in source
    assert 'resource "random_password" "clinic_recall_privacy_db"' in source
    assert 'name         = "clinic-recall-privacy-db-password"' in source
    assert 'name = "clinic-recall-privacy-db-connection-string"' in source
    assert "user=clinic_recall_privacy" in source
    for job in (rights, retention):
        assert 'name        = "CLINIC_RECALL_PRIVACY_DATABASE_URL"' in job
        assert 'secret_name = "privacy-postgres"' in job
        assert "clinic_recall_privacy_db_role_ready" in job
        assert (
            "clinic_recall_privacy_db_role_ready_epoch == "
            "var.clinic_recall_privacy_db_password_rotation_epoch"
        ) in job
        assert 'name  = "CLINIC_RECALL_DATABASE_URL"' not in job


def test_privacy_role_helper_is_blocking_secret_safe_and_verifies_rls_flags() -> None:
    helper = PRIVACY_ROLE_HELPER.read_text(encoding="utf-8")
    postprovision = POSTPROVISION.read_text(encoding="utf-8")

    for required in (
        "NOSUPERUSER",
        "NOBYPASSRLS",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOINHERIT",
        "GRANT SELECT ON clinic",
        "GRANT UPDATE (id) ON patient, outreach_job",
        "GRANT UPDATE (content) ON interaction",
        "GRANT UPDATE (recording_status, recording_stop_requested_at)",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON rights_request, rights_target",
        "REVOKE ALL ON clinic_identity_mapping, clinic_phone_number",
        "TF_VAR_clinic_recall_privacy_db_role_ready true",
        "TF_VAR_clinic_recall_privacy_db_role_ready_epoch",
        "${ROLE_NAME}|f|f|f|f",
    ):
        assert required in helper
    assert "ON ALL TABLES" not in helper
    assert "ALTER DEFAULT PRIVILEGES" not in helper
    assert "privacy_password" not in "\n".join(
        line for line in helper.splitlines() if "printf" in line or "echo" in line
    )
    assert "task_configure_privacy_db_role" in postprovision
    assert "task_configure_privacy_db_role || true" not in postprovision
    assert "task_sync_appconfig || true" not in postprovision
    assert postprovision.index("task_configure_privacy_db_role") < postprovision.index(
        "task_sync_appconfig\n"
    )
    sync = APPCONFIG_SYNC.read_text(encoding="utf-8")
    assert (
        'set_kv_ref "app/postgres/privacy-connection-string" '
        '"clinic-recall-privacy-db-connection-string"'
    ) in sync
    assert (
        '"app/postgres/privacy-connection-string": '
        '"CLINIC_RECALL_PRIVACY_DATABASE_URL"'
    ) in APPCONFIG_PROVIDER.read_text(encoding="utf-8")


def test_appconfig_keeps_activation_job_local_and_hmac_in_key_vault() -> None:
    provider = APPCONFIG_PROVIDER.read_text(encoding="utf-8")
    sync = APPCONFIG_SYNC.read_text(encoding="utf-8")

    assert '"app/clinic-recall/rights/enabled"' not in provider
    assert '"app/clinic-recall/retention/scheduler-enabled"' not in provider
    assert 'set_kv "app/clinic-recall/rights/enabled"' not in sync
    assert 'set_kv "app/clinic-recall/retention/scheduler-enabled"' not in sync
    assert (
        'set_kv_ref "app/clinic-recall/rights/hmac-key" '
        '"clinic-recall-rights-hmac-key"'
    ) in sync
    assert 'set_kv "app/clinic-recall/rights/hmac-key"' not in sync
    assert "CLINIC_RECALL_RIGHTS_HMAC_KEY:-" not in sync
    assert "CLINIC_RECALL_RIGHTS_HMAC_PREVIOUS_KEYS_JSON:-" not in sync


def test_policy_metadata_is_validated_and_synchronized_without_defaults() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    provider = APPCONFIG_PROVIDER.read_text(encoding="utf-8")
    sync = APPCONFIG_SYNC.read_text(encoding="utf-8")

    for variable in (
        "clinic_recall_rights_policy_version",
        "clinic_recall_rights_approval_evidence_sha256",
        "clinic_recall_rights_request_due_seconds",
        "clinic_recall_rights_residual_approvals_json",
        "clinic_recall_retention_policy_version",
        "clinic_recall_retention_approval_evidence_sha256",
        "clinic_recall_retention_policy_approved_at",
        "clinic_recall_retention_policy_effective_at",
        "clinic_recall_retention_policy_expires_at",
        "clinic_recall_retention_retain_for_seconds",
        "clinic_recall_retention_request_due_seconds",
    ):
        assert f'variable "{variable}"' in source
    for key in (
        "app/clinic-recall/rights/policy-version",
        "app/clinic-recall/rights/approval-evidence-sha256",
        "app/clinic-recall/rights/request-due-seconds",
        "app/clinic-recall/rights/residual-approvals-json",
        "app/clinic-recall/retention/policy-version",
        "app/clinic-recall/retention/approval-evidence-sha256",
        "app/clinic-recall/retention/policy-approved-at",
        "app/clinic-recall/retention/policy-effective-at",
        "app/clinic-recall/retention/policy-expires-at",
        "app/clinic-recall/retention/retain-for-seconds",
        "app/clinic-recall/retention/request-due-seconds",
    ):
        assert f'"{key}"' in provider
        assert key in sync
    assert "must be timezone-aware RFC3339" in sync
    assert "must be a lowercase SHA-256 digest" in sync
    assert "residual-approvals-json is invalid" in sync
    assert 'output "CLINIC_RECALL_RIGHTS_RESIDUAL_APPROVALS_JSON"' in source
    assert (
        "var.clinic_recall_rights_request_due_seconds > 0 ? "
        "tostring(var.clinic_recall_rights_request_due_seconds) : \"\""
    ) in source
    assert (
        "var.clinic_recall_retention_retain_for_seconds > 0 ? "
        "tostring(var.clinic_recall_retention_retain_for_seconds) : \"\""
    ) in source