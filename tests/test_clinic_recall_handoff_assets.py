"""Static deployment and monitoring contracts for PR-12 handoffs."""

from __future__ import annotations

from pathlib import Path

JOB_TERRAFORM = Path("infra/terraform/clinic-recall-jobs.tf")
MONITORING_TERRAFORM = Path("infra/terraform/monitoring.tf")
APPCONFIG_PROVIDER = Path("apps/artagent/backend/config/appconfig_provider.py")
APPCONFIG_SYNC = Path("devops/scripts/azd/helpers/sync-appconfig.sh")


def _resource_block(source: str, name: str) -> str:
    marker = f'resource "azurerm_container_app_job" "{name}"'
    start = source.index(marker)
    next_resource = source.find('\nresource "', start + len(marker))
    return source[start:] if next_resource == -1 else source[start:next_resource]


def test_handoff_ageing_job_is_scheduled_finite_and_off_by_default() -> None:
    source = JOB_TERRAFORM.read_text(encoding="utf-8")
    job = _resource_block(source, "clinic_recall_handoff_ageing")

    assert 'variable "clinic_recall_handoff_ageing_job_enabled"' in source
    assert 'variable "clinic_recall_handoff_ageing_execution_enabled"' in source
    assert "count = var.clinic_recall_handoff_ageing_job_enabled ? 1 : 0" in job
    assert "schedule_trigger_config" in job
    assert 'default     = "*/5 * * * *"' in source
    assert "parallelism              = 1" in job
    assert "replica_completion_count = 1" in job
    assert "replica_retry_limit        = 0" in job
    assert '"src.clinic_recall.handoff_ageing"' in job
    assert 'name  = "CLINIC_RECALL_HANDOFF_AGEING_ENABLED"' in job
    assert "var.clinic_recall_durable_sms_worker_image" in job
    assert "var.clinic_recall_durable_sms_worker_version" in job
    assert "var.clinic_recall_durable_sms_clinic_id" in job
    assert "send_email" not in job
    assert "send_sms" not in job


def test_handoff_switches_use_non_secret_appconfig_keys() -> None:
    provider = APPCONFIG_PROVIDER.read_text(encoding="utf-8")
    sync = APPCONFIG_SYNC.read_text(encoding="utf-8")
    expected = {
        "app/clinic-recall/handoff-notification-enabled": "CLINIC_RECALL_HANDOFF_NOTIFICATION_ENABLED",
        "app/clinic-recall/handoff-ageing-enabled": "CLINIC_RECALL_HANDOFF_AGEING_ENABLED",
        "app/clinic-recall/handoff-delivery-callback-enabled": "CLINIC_RECALL_HANDOFF_DELIVERY_CALLBACK_ENABLED",
    }
    for key, environment_name in expected.items():
        assert f'"{key}": "{environment_name}"' in provider
        assert environment_name in sync
        assert key in sync
        assert f'set_kv_ref "{key}"' not in sync
    assert 'set_kv "$handoff_key" "$handoff_value"' in sync


def test_handoff_alerts_reuse_existing_action_group_and_closed_event_names() -> None:
    source = MONITORING_TERRAFORM.read_text(encoding="utf-8")

    assert 'resource "azurerm_monitor_action_group" "clinic_recall"' in source
    assert "length(var.monitor_alert_email_receivers) == 0 ? 0 : 1" in source
    for key in (
        "handoff_sla_breach",
        "handoff_destination_unavailable",
        "handoff_notification_ambiguity",
        "handoff_alternate_page_requested",
        "handoff_programme_pause",
    ):
        assert f"    {key} = {{" in source
    for event_name in (
        "handoff.sla.breach",
        "handoff.notification.outcome",
        "handoff.alternate.requested",
        "handoff.programme.pause",
    ):
        assert event_name in source
    assert "receipt_id" not in source.lower()
    assert "provider_resource_id" not in source.lower()
    assert "email_address" not in "\n".join(
        line for line in source.splitlines() if "handoff." in line
    )