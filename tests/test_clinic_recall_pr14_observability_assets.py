"""PR-14 pilot observability coverage contract.

One closed registry must bind every required pilot signal to exactly one
approved source event and one alert contract, reuse PR-12 handoff signals,
surface a dedicated Pilot Operations Workbook page, document dry-run and
control-first rollback operations, and ship a dry-run-only rehearsal probe.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

MONITORING_PATH = Path("infra/terraform/monitoring.tf")
JOBS_PATH = Path("infra/terraform/clinic-recall-jobs.tf")
WORKBOOK_PATH = Path("infra/terraform/dashboards/unified-workbook.json.tftpl")
RUNBOOK_PATH = Path("docs/clinic-recall-production-bring-up-runbook.md")
PROBE_PATH = Path("devops/probes/pilot_observability_rehearsal.py")
WORKER_SUMMARY_SOURCES = {
    Path("src/clinic_recall/durable/worker.py"): ("sms_dispatch",),
    Path("src/clinic_recall/durable/call_worker.py"): ("call_dispatch",),
    Path("src/clinic_recall/durable/recording_worker.py"): ("recording_dispatch",),
    Path("src/clinic_recall/durable/rights_worker.py"): (
        "rights_dispatch",
        "rights_reconcile",
    ),
    Path("src/clinic_recall/durable/callbacks.py"): ("callback_reconcile",),
    Path("src/clinic_recall/durable/cliniko_booking_worker.py"): ("cliniko_dispatch",),
    Path("src/clinic_recall/durable/cliniko_booking_reconciler.py"): ("cliniko_reconcile",),
}
JOB_TELEMETRY_BOOTSTRAPS = (
    Path("src/clinic_recall/durable/worker.py"),
    Path("src/clinic_recall/durable/reconcile.py"),
    Path("src/clinic_recall/durable/rights_worker.py"),
    Path("src/clinic_recall/handoff_ageing.py"),
)

REQUIRED_SIGNAL_KEYS = frozenset(
    {
        "ambiguous_external_effect",
        "ambiguous_callback",
        "dead_letter_effect",
        "callback_processing_lag",
        "cliniko_readback_conflict",
        "booking_confirmation_grounding_failure",
        "recording_consent_provider_mismatch",
        "rights_deletion_overdue",
        "handoff_sla_breach",
        "handoff_destination_failure",
        "handoff_alternate_page",
        "handoff_pause",
        "pilot_cohort_invariant_violation",
        "app_configuration_stale_or_missing",
        "release_environment_mismatch",
    }
)

PR12_REUSED_CONTRACTS = {
    "handoff_sla_breach": ("handoff.sla.breach", "handoff_sla_breach"),
    "handoff_destination_failure": (
        "handoff.notification.outcome",
        "handoff_destination_unavailable",
    ),
    "handoff_alternate_page": (
        "handoff.alternate.requested",
        "handoff_alternate_page_requested",
    ),
    "handoff_pause": ("handoff.programme.pause", "handoff_programme_pause"),
}

FORBIDDEN_IDENTIFIER_TOKENS = (
    "patient_id",
    "clinic_id",
    "appointment_id",
    "effect_id",
    "receipt_id",
    "callback_id",
    "provider_resource_id",
    "actor",
    "message_body",
    "phone",
    "email",
    "transcript",
    "payload",
    "release_identity",
    "destination",
    "raw_error",
    "error_message",
    "free_text",
)


def _registry():
    try:
        from src.clinic_recall.observability_registry import (
            PILOT_OBSERVABILITY_REGISTRY,
        )
    except ImportError:
        pytest.fail(
            "PR-14 pilot observability registry is missing "
            "(src/clinic_recall/observability_registry.py); "
            f"uncovered signal keys: {sorted(REQUIRED_SIGNAL_KEYS)}"
        )
    return PILOT_OBSERVABILITY_REGISTRY


def _alert_block(source: str, alert_key: str) -> str:
    marker = f"    {alert_key} = {{"
    assert marker in source, f"monitoring.tf is missing alert {alert_key!r}"
    start = source.index(marker)
    end = source.find("\n    }\n", start)
    assert end != -1, f"unterminated alert block for {alert_key!r}"
    return source[start : end + 6]


def _resource_block(source: str, resource_type: str, name: str) -> str:
    marker = f'resource "{resource_type}" "{name}"'
    start = source.index(marker)
    next_resource = source.find('\nresource "', start + len(marker))
    return source[start:] if next_resource == -1 else source[start:next_resource]


def test_registry_covers_exactly_the_required_pilot_signal_keys() -> None:
    registry = _registry()
    missing = REQUIRED_SIGNAL_KEYS - set(registry)
    assert not missing, f"uncovered PR-14 signal keys: {sorted(missing)}"
    extras = set(registry) - REQUIRED_SIGNAL_KEYS
    assert not extras, f"registry must stay closed; unexpected keys: {sorted(extras)}"


def test_each_signal_key_binds_one_source_event_and_one_alert() -> None:
    registry = _registry()
    from devops.probes.pilot_observability_rehearsal import signal_fixtures
    from src.clinic_recall.telemetry import _EVENT_ATTRIBUTES

    monitoring = MONITORING_PATH.read_text(encoding="utf-8")
    workbook = WORKBOOK_PATH.read_text(encoding="utf-8")
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    fixtures = signal_fixtures()
    alert_keys: list[str] = []
    for key, contract in registry.items():
        assert contract.event, f"{key} has no source event"
        assert contract.alert_key, f"{key} has no alert contract"
        assert contract.owner, f"{key} has no owning module"
        assert contract.source_state, f"{key} has no authoritative source state"
        assert contract.source_freshness, f"{key} has no freshness rule"
        assert contract.runbook_response, f"{key} has no runbook response"
        assert contract.fixture_key == key and key in fixtures
        assert f'"name": "{contract.workbook_view}"' in workbook
        assert "## 7. Pilot observability" in runbook
        assert contract.comparator == "GreaterThan"
        assert contract.threshold == 0
        assert contract.window == "PT15M"
        assert contract.frequency == "PT5M"
        assert contract.event in _EVENT_ATTRIBUTES, (
            f"{key} event {contract.event!r} is not an allow-listed telemetry event"
        )
        assert contract.dimensions <= _EVENT_ATTRIBUTES[contract.event], (
            f"{key} declares dimensions outside the {contract.event!r} allowlist"
        )
        for dimension in contract.dimensions:
            for token in FORBIDDEN_IDENTIFIER_TOKENS:
                assert token not in dimension.lower(), (
                    f"{key} dimension {dimension!r} contains forbidden token {token!r}"
                )
        block = _alert_block(monitoring, contract.alert_key)
        assert contract.event in block, (
            f"alert {contract.alert_key!r} does not query source event {contract.event!r}"
        )
        if not contract.reused:
            assert "| where TimeGenerated > ago(15m)" in block, (
                f"alert {contract.alert_key!r} must bound its own query window"
            )
            assert f"severity     = {contract.severity}" in block
            assert f'frequency    = "{contract.frequency}"' in block
            assert f'window       = "{contract.window}"' in block
            assert f"threshold    = {contract.threshold}" in block
        alert_keys.append(contract.alert_key)
    assert len(alert_keys) == len(set(alert_keys)), (
        "each signal key must own exactly one alert contract"
    )


def test_handoff_signal_keys_reuse_pr12_contracts_without_duplication() -> None:
    registry = _registry()
    for key, (event, alert_key) in PR12_REUSED_CONTRACTS.items():
        contract = registry[key]
        assert contract.reused is True, f"{key} must reuse the PR-12 contract"
        assert contract.event == event, f"{key} must reuse PR-12 event {event!r}"
        assert contract.alert_key == alert_key, f"{key} must reuse PR-12 alert {alert_key!r}"
    for key, contract in registry.items():
        if key not in PR12_REUSED_CONTRACTS:
            assert contract.reused is False, (
                f"{key} must declare a new PR-14 source, not claim reuse"
            )
            assert not contract.event.startswith("handoff."), (
                f"{key} must not duplicate PR-12 handoff semantics"
            )


def test_worker_counter_sources_emit_each_registered_completed_cycle() -> None:
    for path, workers in WORKER_SUMMARY_SOURCES.items():
        source = path.read_text(encoding="utf-8")
        for worker in workers:
            assert f'emit_worker_summary("{worker}", result.as_summary())' in source


def test_short_lived_alert_producer_jobs_configure_azure_monitor_logging() -> None:
    for path in JOB_TELEMETRY_BOOTSTRAPS:
        source = path.read_text(encoding="utf-8")
        assert "configure_job_telemetry()" in source, path
    snapshot = Path("src/clinic_recall/operational_snapshot.py").read_text(encoding="utf-8")
    assert "_bootstrap_runtime_configuration()" in snapshot


def test_alerts_remain_stateful_and_receiver_inert_without_configuration() -> None:
    monitoring = MONITORING_PATH.read_text(encoding="utf-8")
    assert "auto_mitigation_enabled = true" in monitoring
    assert "count = length(var.monitor_alert_email_receivers) == 0 ? 0 : 1" in monitoring
    assert (
        "for_each = length(azurerm_monitor_action_group.clinic_recall) == 0 ? [] : [1]"
        in monitoring
    )


def test_new_alert_queries_use_only_registered_closed_dimensions() -> None:
    registry = _registry()
    monitoring = MONITORING_PATH.read_text(encoding="utf-8")
    for key, contract in registry.items():
        if contract.reused:
            continue
        block = _alert_block(monitoring, contract.alert_key)
        query = block[block.index("<<-KQL") : block.index("KQL\n", block.index("<<-KQL"))]
        properties = set(re.findall(r"Properties\['([a-z0-9_.]+)'\]", query))
        allowed = set(contract.dimensions) | {"microsoft.custom_event.name"}
        assert properties <= allowed, (
            f"alert {contract.alert_key!r} reads attributes outside the "
            f"closed {key} dimensions: {sorted(properties - allowed)}"
        )
        lowered = query.lower()
        for token in FORBIDDEN_IDENTIFIER_TOKENS:
            assert token not in lowered, (
                f"alert {contract.alert_key!r} query must not touch {token!r}"
            )


def test_workbook_has_dedicated_pilot_operations_surface() -> None:
    template = WORKBOOK_PATH.read_text(encoding="utf-8")
    assert '"subTarget": "pilotops"' in template, (
        "unified Workbook is missing the Pilot Operations tab"
    )
    rendered = (
        template.replace("${environment_name}", "staging")
        .replace("${metric_environment_predicate}", "Environment == 'staging'")
        .replace(
            "${workspace_id}",
            "/subscriptions/00000000-0000-0000-0000-000000000000/"
            "resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/log",
        )
    )
    workbook = json.loads(rendered)
    pilot_items = [
        item
        for item in workbook["items"]
        if item.get("conditionalVisibility", {}).get("value") == "pilotops"
    ]
    assert pilot_items, "Pilot Operations tab has no Workbook items"
    encoded = json.dumps(pilot_items).lower()
    for token in ("patient_id", "phone_number", "transcript", "receipt_id"):
        assert token not in encoded


def test_runbook_documents_dry_run_kill_switch_and_control_first_rollback() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    assert "Pilot observability" in runbook, (
        "runbook is missing the PR-14 pilot observability section"
    )
    for marker in (
        "pilot_observability_rehearsal",
        "kill switch",
        "database pause",
        "App Configuration off",
        "Jobs and recording stopped",
        "code/image rollback",
    ):
        assert marker in runbook, f"runbook is missing procedure marker {marker!r}"
    order = [
        runbook.index("database pause"),
        runbook.index("App Configuration off"),
        runbook.index("Jobs and recording stopped"),
        runbook.index("code/image rollback"),
    ]
    assert order == sorted(order), "rollback steps must stay in control-first order"


def test_rehearsal_probe_exists_and_is_dry_run_only() -> None:
    assert PROBE_PATH.exists(), "PR-14 rehearsal probe is missing"
    probe = PROBE_PATH.read_text(encoding="utf-8")
    assert "DRY_RUN_ONLY = True" in probe, "rehearsal must be dry-run only"
    for forbidden in (
        "--live",
        "import requests",
        "import httpx",
        "import twilio",
        "from twilio",
        "import azure",
        "from azure",
        "DefaultAzureCredential",
    ):
        assert forbidden not in probe, f"rehearsal probe must stay offline; found {forbidden!r}"


def test_snapshot_job_is_finite_read_only_and_double_gated_off_by_default() -> None:
    source = JOBS_PATH.read_text(encoding="utf-8")
    job = _resource_block(
        source,
        "azurerm_container_app_job",
        "clinic_recall_operational_snapshot",
    )

    assert 'variable "clinic_recall_operational_snapshot_job_enabled"' in source
    assert 'variable "clinic_recall_operational_snapshot_execution_enabled"' in source
    assert "count = var.clinic_recall_operational_snapshot_job_enabled ? 1 : 0" in job
    assert "default     = false" in source
    assert "schedule_trigger_config" in job
    assert "parallelism              = 1" in job
    assert "replica_completion_count = 1" in job
    assert "replica_retry_limit        = 0" in job
    assert '"src.clinic_recall.operational_snapshot"' in job
    assert 'name  = "CLINIC_RECALL_OPERATIONAL_SNAPSHOT_ENABLED"' in job
    assert "var.clinic_recall_operational_snapshot_execution_enabled" in job
    assert "var.clinic_recall_durable_sms_clinic_id" in job
    assert "--lookback-hours" in job
    for mutation in ("send_sms", "send_email", "place_call", "start_recording"):
        assert mutation not in job
