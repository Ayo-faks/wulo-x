from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from devops.agentops.emit_metric_rows import (
    MetricContext,
    MetricRowError,
    metric_rows_from_agentops_result,
    metric_rows_from_assert_summary,
    metric_rows_from_doctor_summary,
    metric_rows_from_evidence,
    metric_rows_from_probe,
    metric_rows_from_red_team_summary,
)

BASELINE_PATH = Path(".agentops/baselines/hybrid-rollout-scoreboard.json")


@pytest.fixture
def baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _evidence() -> dict:
    return {
        "schema_version": 2,
        "source": "agentops",
        "metrics": {
            "recall_smoke_core": 0.9,
            "safe_clinical_boundary": 1.0,
        },
        "provenance": {
            "phase": 8,
            "environment": "staging",
            "generated_at": "2026-07-17T10:00:00Z",
            "git_head": "a" * 40,
            "model_version": "gpt-4o-mini:2024-07-18",
            "classifier_prompt_sha256": "b" * 64,
            "classifier_schema_sha256": "c" * 64,
            "source_artifact_id": "hosted-agentops",
            "source_artifact_sha256": "d" * 64,
            "hosted_agents": {
                "recall": {"eval_run_id": "evalrun_recall"},
            },
        },
    }


def test_evidence_rows_are_allow_listed_and_phase_aware(baseline: dict) -> None:
    rows = metric_rows_from_evidence(
        _evidence(),
        baseline=baseline,
        context=MetricContext(
            workflow_name="AgentOps PR",
            workflow_run_url="https://github.example/actions/runs/1",
            image_tag="sha-abc",
        ),
    )

    by_name = {row["MetricName"]: row for row in rows}
    smoke = by_name["recall_smoke_core"]
    assert smoke["Environment"] == "staging"
    assert smoke["Suite"] == "recall"
    assert smoke["Comparator"] == ">="
    assert smoke["Threshold"] == 0.8
    assert smoke["Passed"] is True
    assert smoke["EvalRunId"] == "evalrun_recall"
    assert len(smoke["EvidenceFingerprint"]) == 64
    assert by_name["safe_clinical_boundary"]["Threshold"] == 1.0


def test_evidence_rows_do_not_copy_arbitrary_content(baseline: dict) -> None:
    document = _evidence()
    document["prompt"] = "patient transcript must never be emitted"

    with pytest.raises(MetricRowError, match="aggregate-only schema"):
        metric_rows_from_evidence(document, baseline=baseline)

    document = _evidence()
    document["metrics"]["patient_phone"] = 123.0
    with pytest.raises(MetricRowError, match="non-allow-listed"):
        metric_rows_from_evidence(document, baseline=baseline)


def test_evidence_fingerprint_is_stable_across_workflow_reruns(baseline: dict) -> None:
    first = metric_rows_from_evidence(
        _evidence(),
        baseline=baseline,
        context=MetricContext(workflow_run_url="https://github.example/actions/runs/1"),
    )
    second = metric_rows_from_evidence(
        copy.deepcopy(_evidence()),
        baseline=baseline,
        context=MetricContext(workflow_run_url="https://github.example/actions/runs/1?attempt=2"),
    )

    assert [row["EvidenceFingerprint"] for row in first] == [
        row["EvidenceFingerprint"] for row in second
    ]


def test_probe_rows_emit_verdict_and_bounded_aggregates() -> None:
    rows = metric_rows_from_probe(
        {
            "schema_version": 1,
            "scenario": "urgent_hard_stop",
            "run_uuid": "run-1",
            "passed": False,
            "reason_codes": ["semantic_p95_above_600_ms"],
            "window": {
                "start": "2026-07-17T09:59:00Z",
                "end": "2026-07-17T10:00:00Z",
            },
            "fingerprints": {"hard_stop_forward_blocked": True},
            "cardinalities": {"escalation_count": 1},
            "metrics": {
                "severity_3_or_higher": 0,
                "semantic_p95_ms": 650.0,
                "active_tasks_max_per_anchor_kind": None,
            },
        },
        context=MetricContext(environment="staging"),
    )

    by_name = {row["MetricName"]: row for row in rows}
    assert by_name["probe.scorecard_passed"]["Passed"] is False
    assert by_name["probe.scorecard_passed"]["ReasonCode"] == (
        "semantic_p95_above_600_ms"
    )
    assert by_name["probe.semantic_p95_ms"]["Comparator"] == "<="
    assert by_name["probe.semantic_p95_ms"]["Passed"] is False
    assert by_name["probe.severity_3_or_higher"]["Passed"] is True
    assert by_name["probe.cardinality.escalation_count"]["Threshold"] is None


def test_current_agentops_summary_emits_only_allow_listed_aggregates(baseline: dict) -> None:
    rows = metric_rows_from_agentops_result(
        {
            "finished_at": "2026-07-17T10:00:00Z",
            "duration_seconds": 42.0,
            "target": {"raw": "inbound-assistant:8"},
            "aggregate_metrics": {
                "inbound-smoke-core": 1.0,
                "coherence": 0.8,
                "fluency": 1.0,
                "untrusted_new_metric": 999,
            },
            "config": {
                "azd_evaluation": {"run_id": "evalrun_1"},
                "prompt": "must never be copied",
            },
        },
        baseline=baseline,
        context=MetricContext(environment="staging"),
    )

    by_name = {row["MetricName"]: row for row in rows}
    assert set(by_name) == {
        "agentops.duration_seconds",
        "coherence",
        "fluency",
        "inbound_smoke_core",
    }
    assert by_name["inbound_smoke_core"]["Suite"] == "inbound"
    assert by_name["inbound_smoke_core"]["EvalRunId"] == "evalrun_1"
    assert "must never be copied" not in json.dumps(rows)


def test_current_governance_summaries_emit_counts_without_prose(baseline: dict) -> None:
    context = MetricContext(environment="staging", suite="recall")
    assert_rows = metric_rows_from_assert_summary(
        {
            "suite": "recall-agent-v1",
            "run_id": "ci",
            "failed_cases": 0,
            "pass_rate": 1.0,
            "total_cases": 15,
            "skipped_cases": 0,
            "case_output": "must never be copied",
        },
        baseline=baseline,
        context=context,
    )
    red_team_rows = metric_rows_from_red_team_summary(
        {
            "attack_success_rate": 0.1,
            "successful_attacks": 1,
            "total_attempts": 10,
            "per_category": {
                "violence": {
                    "attack_success_rate": 0.1,
                    "successful": 1,
                    "total": 10,
                }
            },
            "raw_attack": "must never be copied",
        },
        baseline=baseline,
        context=context,
    )
    doctor_rows = metric_rows_from_doctor_summary(
        {
            "generated_at": "2026-07-17T10:00:00Z",
            "status": "ready_with_warnings",
            "target": "recall-agent:7",
            "blockers": [],
            "warnings": ["warning prose must never be copied"],
            "ready": ["ready prose must never be copied"],
        },
        context=context,
    )

    encoded = json.dumps(assert_rows + red_team_rows + doctor_rows)
    assert "must never be copied" not in encoded
    assert {row["MetricName"] for row in doctor_rows} == {
        "doctor.ready",
        "doctor.blocker_count",
        "doctor.warning_count",
        "doctor.ready_check_count",
    }
    assert next(
        row for row in red_team_rows if row["MetricName"] == "recall_red_team_asr"
    )["Passed"] is False


def test_zero_case_assert_summary_omits_undefined_pass_rate(baseline: dict) -> None:
    rows = metric_rows_from_assert_summary(
        {
            "suite": "recall-agent-v1",
            "run_id": "ci",
            "failed_cases": 0,
            "pass_rate": None,
            "total_cases": 0,
            "skipped_cases": 0,
            "metrics": {},
            "dimension_summary": {},
        },
        baseline=baseline,
        context=MetricContext(environment="dev", suite="recall"),
    )

    by_name = {row["MetricName"]: row for row in rows}
    assert set(by_name) == {
        "recall_assert_violations",
        "assert.total_cases",
        "assert.skipped_cases",
    }
    assert by_name["recall_assert_violations"]["Passed"] is True