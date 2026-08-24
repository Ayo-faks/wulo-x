"""Rehearsal contract tests for the PR-14 pilot observability probe."""

from __future__ import annotations

import sys

from devops.probes.pilot_observability_rehearsal import (
    DRY_RUN_ONLY,
    evaluate_apptraces_query,
    load_apptraces_alert_queries,
    rehearse_alert_predicates,
    rehearse_kill_switch_and_duplicate_telemetry,
    rehearse_rollback_order,
    rehearse_signal_distinctness,
    run_rehearsal,
    signal_fixtures,
    zero_operation_report,
)
from src.clinic_recall.observability_registry import PILOT_OBSERVABILITY_REGISTRY


def test_probe_is_dry_run_only() -> None:
    assert DRY_RUN_ONLY is True


def test_every_registered_alert_query_is_locally_evaluable() -> None:
    queries = load_apptraces_alert_queries()
    for contract in PILOT_OBSERVABILITY_REGISTRY.values():
        assert contract.alert_key in queries
        assert evaluate_apptraces_query(queries[contract.alert_key], []) == []


def test_every_alert_fires_on_violation_and_resolves_on_healthy() -> None:
    results = rehearse_alert_predicates()
    assert set(results) == set(PILOT_OBSERVABILITY_REGISTRY)
    for key, checks in results.items():
        assert checks["fires_on_violation"], f"{key} did not fire on violation"
        assert checks["resolves_on_healthy"], f"{key} matched a healthy fixture"


def test_fixtures_cover_every_registered_signal_key() -> None:
    assert set(signal_fixtures()) == set(PILOT_OBSERVABILITY_REGISTRY)


def test_dead_letter_reconciliation_policy_and_delivery_stay_distinct() -> None:
    distinctness = rehearse_signal_distinctness()
    assert distinctness["dead_letter_not_ambiguity"]
    assert distinctness["ambiguity_not_dead_letter"]
    assert distinctness["policy_denial_not_cohort_violation"]
    assert distinctness["delivery_is_not_acknowledgement"]


def test_stale_configuration_alerts_while_fresh_configuration_does_not() -> None:
    queries = load_apptraces_alert_queries()
    fixtures = signal_fixtures()["app_configuration_stale_or_missing"]
    stale = evaluate_apptraces_query(queries["pilot_configuration_stale"], fixtures["violating"])
    fresh = evaluate_apptraces_query(queries["pilot_configuration_stale"], fixtures["healthy"])
    assert len(stale) == 2
    assert fresh == []


def test_kill_switch_rehearsal_leaves_safety_controls_on() -> None:
    checks = rehearse_kill_switch_and_duplicate_telemetry()
    assert checks["gate_allowed_before_pause"]
    assert checks["database_pause_denies_outreach"]
    assert checks["pause_is_recorded_not_destructive"]


def test_duplicate_telemetry_does_not_duplicate_business_actions() -> None:
    checks = rehearse_kill_switch_and_duplicate_telemetry()
    assert checks["duplicate_telemetry_no_business_writes"]
    assert checks["duplicate_telemetry_identical_aggregates"]
    assert checks["telemetry_emitted_twice"]


def test_rollback_order_is_control_first() -> None:
    rollback = rehearse_rollback_order()
    assert rollback["all_steps_documented"]
    assert rollback["control_first_order"]


def test_rehearsal_reports_zero_provider_and_azure_operations() -> None:
    operations = zero_operation_report(frozenset(sys.modules))
    assert set(operations.values()) == {0}


def test_full_rehearsal_verdict_passes_and_labels_evidence_scope() -> None:
    verdict = run_rehearsal()
    assert verdict["passed"] is True
    assert verdict["dry_run_only"] is True
    assert "not Azure evidence" in str(verdict["evidence_scope"])
