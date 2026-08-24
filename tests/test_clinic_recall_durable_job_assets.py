"""Static safety contract for the PR-01 Container Apps Job."""

from __future__ import annotations

from pathlib import Path

JOB_TERRAFORM = Path("infra/terraform/clinic-recall-jobs.tf")
APPCONFIG_PROVIDER = Path("apps/artagent/backend/config/appconfig_provider.py")
APPCONFIG_SYNC = Path("devops/scripts/azd/helpers/sync-appconfig.sh")


def _resource_block(source: str, name: str) -> str:
    marker = f'resource "azurerm_container_app_job" "{name}"'
    start = source.index(marker)
    next_resource = source.find('\nresource "', start + len(marker))
    return source[start:] if next_resource == -1 else source[start:next_resource]


def test_durable_sms_job_is_manual_finite_and_off_by_default() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    manual_job = _resource_block(source, "clinic_recall_sms")

    assert 'resource "azurerm_container_app_job" "clinic_recall_sms"' in source
    assert 'variable "clinic_recall_durable_sms_job_enabled"' in source
    assert 'variable "clinic_recall_durable_sms_dispatch_enabled"' in source
    assert source.count("default     = false") >= 2
    assert "manual_trigger_config" in manual_job
    assert "schedule_trigger_config" not in manual_job
    assert "event_trigger_config" not in manual_job
    assert "parallelism              = 1" in manual_job
    assert "replica_completion_count = 1" in manual_job
    assert "replica_retry_limit        = 0" in manual_job
    assert "replica_timeout_in_seconds = var.clinic_recall_durable_sms_job_timeout_seconds" in manual_job
    assert '"src.clinic_recall.durable.worker"' in manual_job
    assert 'name  = "CLINIC_RECALL_DURABLE_SMS_ENABLED"' in manual_job
    assert "azurerm_user_assigned_identity.backend.id" in manual_job
    assert "azurerm_container_registry.main.login_server" in manual_job


def test_durable_sms_job_requires_explicit_image_and_internal_clinic_scope() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    manual_job = _resource_block(source, "clinic_recall_sms")

    assert 'variable "clinic_recall_durable_sms_worker_image"' in source
    assert 'variable "clinic_recall_durable_sms_worker_version"' in source
    assert 'variable "clinic_recall_durable_sms_clinic_id"' in source
    assert "var.clinic_recall_durable_sms_worker_image" in source
    assert manual_job.count("var.clinic_recall_durable_sms_worker_version") == 3
    assert "var.clinic_recall_durable_sms_clinic_id" in manual_job
    assert 'data.external.git_commit.result["commit"]' not in source
    assert "precondition" in source
    assert "clinic-live-test" not in source
    assert "+44" not in source


def test_scheduled_planner_job_is_finite_independently_gated_and_off_by_default() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    planner = _resource_block(source, "clinic_recall_planner")

    assert 'variable "clinic_recall_cadence_job_enabled"' in source
    assert 'variable "clinic_recall_cadence_execution_enabled"' in source
    assert "count = var.clinic_recall_cadence_job_enabled ? 1 : 0" in planner
    assert "schedule_trigger_config" in planner
    assert "manual_trigger_config" not in planner
    assert "cron_expression" in planner
    assert "parallelism              = 1" in planner
    assert "replica_completion_count = 1" in planner
    assert "replica_retry_limit        = 0" in planner
    assert '"src.clinic_recall.durable.planner"' in planner
    assert 'name  = "CLINIC_RECALL_CADENCE_PLANNING_ENABLED"' in planner
    assert 'name  = "CLINIC_RECALL_DURABLE_SMS_ENABLED"' not in planner
    assert "send_email" not in planner
    assert "send_sms" not in planner
    assert "CallInitiator" not in planner
    assert "CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED" not in planner
    assert "CLINIC_RECALL_CALLBACK_RECONCILIATION_ENABLED" not in planner


def test_scheduled_planner_reuses_reviewed_image_identity_and_validates_bounds() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    planner = _resource_block(source, "clinic_recall_planner")

    assert "var.clinic_recall_durable_sms_worker_image" in planner
    assert planner.count("var.clinic_recall_durable_sms_worker_version") == 3
    assert "var.clinic_recall_durable_sms_clinic_id" in planner
    assert 'variable "clinic_recall_cadence_schedule_utc"' in source
    assert 'default     = "0 * * * *"' in source
    assert "clinic_recall_cadence_batch_limit must be between 1 and 100" in source
    assert "clinic_recall_cadence_window_minutes must be between 1 and 1440" in source
    assert "clinic_recall_cadence_config_max_age_seconds must be between 1 and 3600" in source


def test_cadence_gate_has_non_secret_appconfig_mapping_and_sync() -> None:
    provider = APPCONFIG_PROVIDER.read_text(encoding="utf-8")
    sync = APPCONFIG_SYNC.read_text(encoding="utf-8")
    expected = {
        "app/clinic-recall/cadence-planning-enabled": "CLINIC_RECALL_CADENCE_PLANNING_ENABLED",
        "app/clinic-recall/cadence-config-refreshed-at": "CLINIC_RECALL_CADENCE_CONFIG_REFRESHED_AT",
        "app/clinic-recall/cadence-config-max-age-seconds": "CLINIC_RECALL_CADENCE_CONFIG_MAX_AGE_SECONDS",
    }
    for key, environment_name in expected.items():
        assert f'"{key}": "{environment_name}"' in provider
        assert f'set_kv "{key}"' in sync
        assert environment_name in sync
    assert "set_kv_ref \"app/clinic-recall/cadence" not in sync


def test_durable_call_job_is_manual_finite_independent_and_off_by_default() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    call_job = _resource_block(source, "clinic_recall_call")

    assert 'resource "azurerm_container_app_job" "clinic_recall_call"' in source
    assert 'variable "clinic_recall_durable_call_job_enabled"' in source
    assert 'variable "clinic_recall_durable_call_dispatch_enabled"' in source
    assert "count = var.clinic_recall_durable_call_job_enabled ? 1 : 0" in call_job
    assert "manual_trigger_config" in call_job
    assert "schedule_trigger_config" not in call_job
    assert "event_trigger_config" not in call_job
    assert "parallelism              = 1" in call_job
    assert "replica_completion_count = 1" in call_job
    assert "replica_retry_limit        = 0" in call_job
    assert (
        "replica_timeout_in_seconds = var.clinic_recall_durable_call_job_timeout_seconds"
        in call_job
    )
    assert '"src.clinic_recall.durable.call_worker"' in call_job
    assert 'name  = "CLINIC_RECALL_DURABLE_CALL_ENABLED"' in call_job
    assert "var.clinic_recall_durable_call_dispatch_enabled" in call_job
    assert 'name  = "CLINIC_RECALL_DURABLE_CALL_PROVIDER"' in call_job
    assert 'value = "twilio"' in call_job
    assert "CLINIC_RECALL_DURABLE_SMS_ENABLED" not in call_job
    assert "CLINIC_RECALL_CADENCE_PLANNING_ENABLED" not in call_job
    assert "CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED" not in call_job
    assert "CLINIC_RECALL_CALLBACK_RECONCILIATION_ENABLED" not in call_job


def test_durable_call_job_reuses_provenance_and_excludes_unsafe_commands() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    call_job = _resource_block(source, "clinic_recall_call")
    lowered = call_job.lower()

    assert 'variable "clinic_recall_durable_call_worker_image"' in source
    assert 'variable "clinic_recall_durable_call_worker_version"' in source
    assert 'variable "clinic_recall_durable_call_clinic_id"' in source
    assert "var.clinic_recall_durable_call_worker_image" in call_job
    assert call_job.count("var.clinic_recall_durable_call_worker_version") == 3
    assert "var.clinic_recall_durable_call_clinic_id" in call_job
    assert "clinic_recall_durable_sms_worker_image" not in call_job
    assert "clinic_recall_durable_sms_worker_version" not in call_job
    assert "clinic_recall_durable_sms_clinic_id" not in call_job
    assert "azurerm_user_assigned_identity.backend.id" in call_job
    assert "azurerm_container_registry.main.login_server" in call_job
    assert 'variable "clinic_recall_durable_call_job_timeout_seconds"' in source
    assert "clinic_recall_durable_call_job_timeout_seconds must be between 60 and 900" in source
    assert 'variable "clinic_recall_durable_call_batch_limit"' in source
    assert "clinic_recall_durable_call_batch_limit must be between 1 and 50" in source
    assert 'output "CLINIC_RECALL_DURABLE_CALL_JOB_NAME"' in source
    for forbidden in (
        "record_call",
        "recordingstatuscallback",
        "recording_status_callback",
        "email",
        "voicemail",
        "asyncamd",
        "async_amd",
    ):
        assert forbidden not in lowered
    assert lowered.count("clinic_recall_pilot_recording_enabled") == 2


def test_durable_call_gate_has_non_secret_appconfig_mapping_and_sync() -> None:
    provider = APPCONFIG_PROVIDER.read_text(encoding="utf-8")
    sync = APPCONFIG_SYNC.read_text(encoding="utf-8")
    expected = {
        "app/clinic-recall/durable-call-enabled": "CLINIC_RECALL_DURABLE_CALL_ENABLED",
        "app/clinic-recall/durable-call-provider": "CLINIC_RECALL_DURABLE_CALL_PROVIDER",
    }
    for key, environment_name in expected.items():
        assert f'"{key}": "{environment_name}"' in provider
        assert environment_name in sync
        assert f'set_kv "{key}"' in sync
        assert f'set_kv_ref "{key}"' not in sync


def test_durable_recording_gate_has_non_secret_appconfig_mapping_and_sync() -> None:
    provider = APPCONFIG_PROVIDER.read_text(encoding="utf-8")
    sync = APPCONFIG_SYNC.read_text(encoding="utf-8")
    expected = {
        "app/clinic-recall/durable-recording-enabled": "CLINIC_RECALL_DURABLE_RECORDING_ENABLED",
        "app/clinic-recall/durable-recording-provider": "CLINIC_RECALL_DURABLE_RECORDING_PROVIDER",
    }
    for key, environment_name in expected.items():
        assert f'"{key}": "{environment_name}"' in provider
        assert f'set_kv "{key}"' in sync
        assert f'set_kv_ref "{key}"' not in sync


def test_durable_recording_job_is_scheduled_finite_independent_and_off_by_default() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    recording_job = _resource_block(source, "clinic_recall_recording")

    assert 'variable "clinic_recall_durable_recording_job_enabled"' in source
    assert 'variable "clinic_recall_durable_recording_dispatch_enabled"' in source
    assert (
        "count = var.clinic_recall_durable_recording_job_enabled ? 1 : 0"
        in recording_job
    )
    assert "schedule_trigger_config" in recording_job
    assert "manual_trigger_config" not in recording_job
    assert "event_trigger_config" not in recording_job
    assert "cron_expression" in recording_job
    assert "parallelism              = 1" in recording_job
    assert "replica_completion_count = 1" in recording_job
    assert "replica_retry_limit        = 0" in recording_job
    assert '"src.clinic_recall.durable.recording_worker"' in recording_job
    assert 'name  = "CLINIC_RECALL_DURABLE_RECORDING_ENABLED"' in recording_job
    assert 'name  = "CLINIC_RECALL_DURABLE_RECORDING_PROVIDER"' in recording_job
    assert 'value = "twilio"' in recording_job
    assert "CLINIC_RECALL_DURABLE_CALL_ENABLED" not in recording_job
    assert "CLINIC_RECALL_DURABLE_SMS_ENABLED" not in recording_job
    assert "CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED" not in recording_job
    assert "CLINIC_RECALL_CALLBACK_RECONCILIATION_ENABLED" not in recording_job


def test_durable_recording_job_has_exact_provenance_scope_and_stop_inputs() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    recording_job = _resource_block(source, "clinic_recall_recording")

    assert 'variable "clinic_recall_durable_recording_worker_image"' in source
    assert 'variable "clinic_recall_durable_recording_worker_version"' in source
    assert 'variable "clinic_recall_durable_recording_clinic_id"' in source
    assert "var.clinic_recall_durable_recording_worker_image" in recording_job
    assert recording_job.count("var.clinic_recall_durable_recording_worker_version") == 3
    assert "var.clinic_recall_durable_recording_clinic_id" in recording_job
    assert "azurerm_user_assigned_identity.backend.id" in recording_job
    assert "azurerm_container_registry.main.login_server" in recording_job
    assert 'variable "clinic_recall_durable_recording_job_timeout_seconds"' in source
    assert (
        "clinic_recall_durable_recording_job_timeout_seconds must be between 60 and 900"
        in source
    )
    assert 'variable "clinic_recall_durable_recording_batch_limit"' in source
    assert (
        "clinic_recall_durable_recording_batch_limit must be between 1 and 50"
        in source
    )
    assert 'output "CLINIC_RECALL_DURABLE_RECORDING_JOB_NAME"' in source
    assert 'variable "clinic_recall_durable_recording_schedule_utc"' in source
    assert 'default     = "* * * * *"' in source
    for environment_name in (
        "CLINIC_RECALL_PILOT_OUTREACH_ENABLED",
        "CLINIC_RECALL_PILOT_VOICE_ENABLED",
        "CLINIC_RECALL_PILOT_RECORDING_ENABLED",
        "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS",
        "CLINIC_RECALL_PILOT_ENVIRONMENT",
        "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
    ):
        assert f'name  = "{environment_name}"' in recording_job


def test_pilot_controls_have_non_secret_mappings_and_runtime_refresh_evidence() -> None:
    provider = APPCONFIG_PROVIDER.read_text(encoding="utf-8")
    sync = APPCONFIG_SYNC.read_text(encoding="utf-8")
    expected = {
        "app/clinic-recall/pilot/outreach-enabled": "CLINIC_RECALL_PILOT_OUTREACH_ENABLED",
        "app/clinic-recall/pilot/voice-enabled": "CLINIC_RECALL_PILOT_VOICE_ENABLED",
        "app/clinic-recall/pilot/recording-enabled": "CLINIC_RECALL_PILOT_RECORDING_ENABLED",
        "app/clinic-recall/pilot/config-max-age-seconds": "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS",
        "app/clinic-recall/pilot/environment": "CLINIC_RECALL_PILOT_ENVIRONMENT",
        "app/clinic-recall/pilot/release-identity": "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
    }
    for key, environment_name in expected.items():
        assert f'"{key}": "{environment_name}"' in provider
        assert f'set_kv "{key}"' in sync
        assert f'set_kv_ref "{key}"' not in sync
    assert "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT" in provider
    assert 'set_kv "app/clinic-recall/pilot/config-refreshed-at"' not in sync


def test_recording_disclosure_has_non_secret_fail_closed_appconfig_contract() -> None:
    provider = APPCONFIG_PROVIDER.read_text(encoding="utf-8")
    sync = APPCONFIG_SYNC.read_text(encoding="utf-8")
    expected = {
        "app/clinic-recall/recording/disclosure-approved": "CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED",
        "app/clinic-recall/recording/disclosure-text": "CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT",
        "app/clinic-recall/recording/disclosure-version": "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION",
    }
    for key, environment_name in expected.items():
        assert f'"{key}": "{environment_name}"' in provider
        assert f'set_kv "{key}"' in sync
        assert f'set_kv_ref "{key}"' not in sync
    assert "disclosure-approved must be true or false" in sync
    assert "disclosure-text must contain 20 to 500 characters" in sync
    assert "disclosure-version is invalid" in sync
    storage_expected = {
        "app/clinic-recall/recording/blob-account-url": "RECORDINGS_BLOB_ACCOUNT_URL",
        "app/clinic-recall/recording/blob-container": "RECORDINGS_BLOB_CONTAINER",
    }
    for key, environment_name in storage_expected.items():
        assert f'"{key}": "{environment_name}"' in provider
        assert f'set_kv "{key}"' in sync
        assert f'set_kv_ref "{key}"' not in sync
    terraform_outputs = Path("infra/terraform/outputs.tf").read_text(encoding="utf-8")
    assert 'output "RECORDINGS_BLOB_ACCOUNT_URL"' in terraform_outputs
    assert 'output "RECORDINGS_BLOB_CONTAINER"' in terraform_outputs


def test_all_outreach_jobs_receive_independent_pilot_stop_inputs() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    sms = _resource_block(source, "clinic_recall_sms")
    planner = _resource_block(source, "clinic_recall_planner")
    call = _resource_block(source, "clinic_recall_call")

    for block in (sms, planner, call):
        assert 'name  = "CLINIC_RECALL_PILOT_OUTREACH_ENABLED"' in block
        assert 'name  = "CLINIC_RECALL_PILOT_VOICE_ENABLED"' in block
        assert 'name  = "CLINIC_RECALL_PILOT_RECORDING_ENABLED"' in block
        assert 'name  = "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS"' in block
        assert 'name  = "CLINIC_RECALL_PILOT_ENVIRONMENT"' in block
        assert 'name  = "CLINIC_RECALL_PILOT_RELEASE_IDENTITY"' in block
        assert "var.clinic_recall_pilot_release_identity" in block
    assert 'variable "clinic_recall_pilot_outreach_enabled"' in source
    assert 'variable "clinic_recall_pilot_voice_enabled"' in source
    assert 'variable "clinic_recall_pilot_recording_enabled"' in source
    assert source.count("default     = false") >= 9