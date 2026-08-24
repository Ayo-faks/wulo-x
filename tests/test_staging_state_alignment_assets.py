from __future__ import annotations

from pathlib import Path

TERRAFORM = Path("infra/terraform")


def _resource_block(source: str, resource_type: str, name: str) -> str:
    marker = f'resource "{resource_type}" "{name}"'
    start = source.index(marker)
    next_resource = source.find('\nresource "', start + len(marker))
    return source[start:] if next_resource == -1 else source[start:next_resource]


def test_container_memory_uses_provider_canonical_units() -> None:
    containers = (TERRAFORM / "containers.tf").read_text(encoding="utf-8")
    outputs = (TERRAFORM / "outputs.tf").read_text(encoding="utf-8")
    variables = (TERRAFORM / "variables.tf").read_text(encoding="utf-8")

    assert 'normalized_frontend_memory = "1Gi"' in containers
    assert 'default     = "4Gi"' in variables
    assert '"1Gi"' in variables
    assert '"4Gi"' in variables
    assert 'value       = replace(var.container_memory_gb' in outputs


def test_source_declares_only_outputs_materialized_in_staging_state() -> None:
    outputs = (TERRAFORM / "outputs.tf").read_text(encoding="utf-8")

    for name in (
        "AZURE_MONITOR_CI_PUBLISHER_CONFIGURED",
        "AZURE_MONITOR_STREAM_NAME",
        "CLINIC_RECALL_METRICS_TABLE",
    ):
        assert f'output "{name}"' not in outputs


def test_all_container_apps_publish_release_identity() -> None:
    containers = (TERRAFORM / "containers.tf").read_text(encoding="utf-8")
    cardapi_source = (TERRAFORM / "cardapi.tf").read_text(encoding="utf-8")
    frontend = _resource_block(containers, "azurerm_container_app", "frontend")
    backend = _resource_block(containers, "azurerm_container_app", "backend")
    cardapi = _resource_block(cardapi_source, "azurerm_container_app", "cardapi_mcp")

    for block in (frontend, backend, cardapi):
        for name in (
            "SERVICE_NAME",
            "SERVICE_NAMESPACE",
            "ENVIRONMENT",
            "SERVICE_VERSION",
            "GIT_SHA",
            "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
        ):
            assert f'name  = "{name}"' in block


def test_backend_release_provenance_is_terraform_convergent() -> None:
    containers = (TERRAFORM / "containers.tf").read_text(encoding="utf-8")
    jobs = (TERRAFORM / "clinic-recall-jobs.tf").read_text(encoding="utf-8")
    backend = _resource_block(containers, "azurerm_container_app", "backend")

    for name in (
        "SERVICE_VERSION",
        "GIT_SHA",
        "CLINIC_RECALL_PILOT_ENVIRONMENT",
        "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
    ):
        assert f'name  = "{name}"' in backend

    lifecycle = backend[backend.index("lifecycle {") :]
    assert "template[0].container[0].image" in lifecycle
    assert "template[0].container[0].env" not in lifecycle
    assert 'output "CLINIC_RECALL_PILOT_ENVIRONMENT"' in jobs
    assert 'output "CLINIC_RECALL_PILOT_RELEASE_IDENTITY"' in jobs


def test_foundry_model_versions_never_auto_upgrade() -> None:
    for path in (
        TERRAFORM / "modules" / "ai" / "foundry.tf",
        TERRAFORM / "modules" / "aifoundry" / "main.tf",
    ):
        source = path.read_text(encoding="utf-8")
        deployment = _resource_block(source, "azurerm_cognitive_deployment", "model")
        assert 'version_upgrade_option = "NoAutoUpgrade"' in deployment, path


def test_azd_hosted_agents_pin_the_qualified_model_version() -> None:
    source = Path("azure.yaml").read_text(encoding="utf-8")

    assert source.count("host: azure.ai.agent") == 2
    assert source.count('version: "2024-07-18"') == 2
    assert 'version: ""' not in source


def test_postprovision_binds_twilio_callbacks_to_public_waf_host() -> None:
    postprovision = Path("devops/scripts/azd/postprovision.sh").read_text(
        encoding="utf-8"
    )

    for key, path in (
        ("app/sms/twilio/webhook-base-url", ""),
        ("app/sms/twilio/status-callback-url", "/api/v1/sms/twilio"),
        ("app/voice/twilio/twiml-url", "/api/v1/voice/twilio/twiml"),
        ("app/voice/twilio/status-callback-url", "/api/v1/voice/twilio/call-status"),
        (
            "app/voice/twilio/recording-status-callback-url",
            "/api/v1/voice/twilio/recording-status",
        ),
    ):
        assert f'appconfig_set "$endpoint" "{key}" "$backend_url{path}" "$label"' in (
            postprovision
        )
    assert "local count=0 required_count=8" in postprovision
    assert "[[ $count -eq $required_count ]]" in postprovision
    assert 'trigger_config_refresh "$endpoint" "$label"' in postprovision
    assert "($count/$required_count)" in postprovision
    assert "$count/3" not in postprovision
    assert "task_update_urls || true" not in postprovision
    assert "task_sync_appconfig || true" not in postprovision
    assert postprovision.index("task_sync_appconfig\n") < postprovision.index(
        "task_update_urls\n"
    )


def test_gateway_preserves_twilio_rule_without_redundant_tls_minimum() -> None:
    network = (TERRAFORM / "network.tf").read_text(encoding="utf-8")
    gateway = _resource_block(network, "azurerm_application_gateway", "main")

    assert 'name      = "AllowTwilioVoiceWebhookPost"' in network
    for callback_path in (
        "/api/v1/voice/twilio/twiml",
        "/api/v1/voice/twilio/call-status",
        "/api/v1/voice/twilio/recording-status",
    ):
        assert f'"{callback_path}"' in network
    assert 'policy_name = "AppGwSslPolicy20220101S"' in gateway
    assert "min_protocol_version" not in gateway


def test_callback_reconciliation_preserves_disabled_manual_job_posture() -> None:
    jobs = (TERRAFORM / "clinic-recall-jobs.tf").read_text(encoding="utf-8")
    manual_job = _resource_block(jobs, "azurerm_container_app_job", "clinic_recall_sms")

    assert 'variable "clinic_recall_durable_sms_dispatch_enabled"' in jobs
    assert 'default     = false' in jobs
    assert "replica_retry_limit        = 0" in manual_job
    assert "manual_trigger_config" in manual_job
    assert "schedule_trigger_config" not in manual_job
    assert "CLINIC_RECALL_CALLBACK_RECONCILIATION_ENABLED" not in manual_job
    assert 'resource "azurerm_container_app_job" "clinic_recall_reconciliation"' not in jobs


def test_generated_secret_expiry_does_not_roll_on_every_plan() -> None:
    resources = {
        "communication.tf": ("azurerm_key_vault_secret", "acs_connection_string"),
        "data.tf": ("azurerm_key_vault_secret", "cosmos_entra_connection_string"),
        "data.tf#admin": ("azurerm_key_vault_secret", "cosmos_admin_password"),
        "data.tf#connection": ("azurerm_key_vault_secret", "cosmos_connection_string"),
        "postgres.tf": ("azurerm_key_vault_secret", "postgres_admin_password"),
        "postgres.tf#connection": (
            "azurerm_key_vault_secret",
            "postgres_connection_string",
        ),
    }

    for label, (resource_type, name) in resources.items():
        path = TERRAFORM / label.split("#", maxsplit=1)[0]
        block = _resource_block(path.read_text(encoding="utf-8"), resource_type, name)
        assert "ignore_changes = [expiration_date]" in block, label


def test_monitoring_ignores_only_volatile_deployer_tag() -> None:
    monitoring = (TERRAFORM / "monitoring.tf").read_text(encoding="utf-8")
    resources = (
        ("azurerm_monitor_data_collection_endpoint", "clinic_recall_metrics"),
        ("azurerm_monitor_data_collection_rule", "clinic_recall_metrics"),
        ("azurerm_application_insights_workbook", "clinic_recall_unified"),
        ("azurerm_monitor_action_group", "clinic_recall"),
        ("azurerm_monitor_scheduled_query_rules_alert_v2", "clinic_recall"),
        ("azurerm_monitor_scheduled_query_rules_alert_v2", "clinic_recall_budget"),
    )

    for resource_type, name in resources:
        block = _resource_block(monitoring, resource_type, name)
        assert 'ignore_changes = [tags["deployed_by"]]' in block