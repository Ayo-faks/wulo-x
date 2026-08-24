from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from devops.agentops.emit_metric_rows import (
    MetricContext,
    metric_rows_from_agentops_result,
)

BASELINE_PATH = Path(".agentops/baselines/hybrid-rollout-scoreboard.json")
MONITORING_PATH = Path("infra/terraform/monitoring.tf")
WORKBOOK_PATH = Path("infra/terraform/dashboards/unified-workbook.json.tftpl")
APP_CONFIG_PROVIDER_PATH = Path("apps/artagent/backend/config/appconfig_provider.py")
APP_CONFIG_SYNC_PATH = Path("devops/scripts/azd/helpers/sync-appconfig.sh")
WORKFLOWS = (
    Path(".github/workflows/agentops-pr.yml"),
    Path(".github/workflows/agentops-deploy-dev.yml"),
    Path(".github/workflows/agentops-promote-prod.yml"),
    Path(".github/workflows/agentops-scheduled.yml"),
)
DEV_DEPLOY_WORKFLOW = Path(".github/workflows/agentops-deploy-dev.yml")
PROD_PROMOTE_WORKFLOW = Path(".github/workflows/agentops-promote-prod.yml")


def test_emitter_row_matches_log_analytics_table_schema() -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    row = metric_rows_from_agentops_result(
        {
            "finished_at": "2026-07-17T10:00:00Z",
            "target": {"raw": "recall-agent:7"},
            "aggregate_metrics": {"smoke-core": 1.0},
            "config": {"azd_evaluation": {"run_id": "evalrun_1"}},
        },
        baseline=baseline,
        context=MetricContext(environment="staging"),
    )[0]
    monitoring = MONITORING_PATH.read_text(encoding="utf-8")
    table_columns = set(
        re.findall(
            r'\{ name = "([A-Za-z][A-Za-z0-9_]*)", table_type = ',
            monitoring,
        )
    )

    assert table_columns == set(row)
    assert "ClinicRecallMetrics_CL" in monitoring
    assert "Monitoring Metrics Publisher" in monitoring


def test_workbook_has_ten_pages_and_aggregate_only_outcome_queries() -> None:
    rendered = (
        WORKBOOK_PATH.read_text(encoding="utf-8")
        .replace("${environment_name}", "staging")
        .replace(
            "${workspace_id}",
            "/subscriptions/00000000-0000-0000-0000-000000000000/"
            "resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/log",
        )
    )
    workbook = json.loads(rendered)
    tabs = next(item for item in workbook["items"] if item["type"] == 11)["content"][
        "links"
    ]
    tab_values = {tab["subTarget"] for tab in tabs}
    visibility_values = {
        item.get("conditionalVisibility", {}).get("value")
        for item in workbook["items"]
    } - {None}
    encoded = json.dumps(workbook).lower()

    assert len(tab_values) == 10
    assert "pilotops" in tab_values
    assert tab_values == visibility_values
    assert "voice.call.status" in encoded
    assert "outreach.message.sent" in encoded
    assert "patient_id" not in encoded
    assert "transcript" not in encoded
    assert "phone_number" not in encoded


def test_workbook_uses_latest_release_posture_and_unit_safe_reliability_views() -> None:
    template = WORKBOOK_PATH.read_text(encoding="utf-8")
    workbook = json.loads(
        template
        .replace("${environment_name}", "staging")
        .replace("${metric_environment_predicate}", "Environment == 'staging'")
        .replace(
            "${workspace_id}",
            "/subscriptions/00000000-0000-0000-0000-000000000000/"
            "resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/log",
        )
    )
    by_name = {item["name"]: item for item in workbook["items"]}

    assert (
        "summarize arg_max(TimeGenerated, *) by Source, Suite, MetricName"
        in by_name["release-posture"]["content"]["query"]
    )
    assert by_name["release-posture"]["content"]["visualization"] == "tiles"
    assert by_name["request-reliability"]["content"]["title"] == (
        "Request reliability summary"
    )
    assert "visualization" not in by_name["request-reliability"]["content"]
    assert by_name["request-volume-trend"]["content"]["title"] == (
        "Request volume trend"
    )
    assert by_name["request-volume-trend"]["content"]["visualization"] == (
        "timechart"
    )
    assert (
        "summarize Requests=count(), Failures=countif(Success == false) "
        "by bin(TimeGenerated, 15m)"
        in by_name["request-volume-trend"]["content"]["query"]
    )
    assert (
        "p95_ms=percentile(DurationMs, 95), Availability="
        "round(100.0 * countif(Success == true) / count(), 2) "
        "by bin(TimeGenerated, 15m)"
        not in by_name["request-reliability"]["content"]["query"]
    )


def test_agentops_workflows_publish_on_success_or_failure_when_configured() -> None:
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)

        assert isinstance(workflow, dict)
        assert "jobs" in workflow
        assert text.count("Publish aggregate metrics to Azure Monitor") == 1
        assert "always()" in text
        assert "AZURE_MONITOR_INGESTION_ENDPOINT" in text
        assert "AZURE_MONITOR_DCR_RULE_ID" in text
        assert "azure-monitor-ingestion==1.0.0" in text
        assert "monitor-metric-rows.jsonl" in text


def test_foundry_mutation_workflows_require_exact_release_authorization() -> None:
    dev = DEV_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    prod = PROD_PROMOTE_WORKFLOW.read_text(encoding="utf-8")
    f1 = (
        "AUTHORIZE PR-15 F1 STAGING QUALIFICATION: PLAN, PROVISION/DEPLOY, "
        "SYNTHETIC PROVIDER TESTS, HOSTED GOVERNANCE, ALERT RECEIVER/REHEARSAL, "
        "AND ROLLBACK"
    )
    f2 = (
        "AUTHORIZE PR-15 F2 PRODUCTION DARK: REVIEWED PLAN/APPLY, DEPLOY, "
        "MIGRATION, PRODUCTION TEST IDENTITIES, AGENT PROMOTION, "
        "ALERT/KILL/ROLLBACK REHEARSAL; LIMIT 0 AND ALL PATIENT SWITCHES FALSE"
    )

    assert "\n  push:" not in dev
    for source, phrase, gate in (
        (dev, f1, "Verify PR-15 F1 authorization"),
        (prod, f2, "Verify PR-15 F2 authorization"),
    ):
        assert "workflow_dispatch:" in source
        assert "authorization:" in source
        assert phrase in source
        assert source.index(gate) < source.index("Azure login (OIDC)")


def test_optional_cost_rates_flow_through_app_configuration() -> None:
    provider = APP_CONFIG_PROVIDER_PATH.read_text(encoding="utf-8")
    sync = APP_CONFIG_SYNC_PATH.read_text(encoding="utf-8")

    for setting in (
        "GENAI_INPUT_COST_PER_MILLION_TOKENS_USD",
        "GENAI_OUTPUT_COST_PER_MILLION_TOKENS_USD",
    ):
        assert setting in provider
        assert setting in sync
    assert "app/monitoring/genai-input-cost-per-million-tokens-usd" in provider
    assert "app/monitoring/genai-output-cost-per-million-tokens-usd" in provider
    assert "must be non-negative numeric" in sync
