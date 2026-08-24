#!/usr/bin/env python3
"""Emit allow-listed CI and probe evidence as Azure Monitor metric rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from devops.probes.kql_scorecard import (  # noqa: E402
    CARDINALITY_FIELDS,
    FINGERPRINT_FIELDS,
    METRIC_FIELDS,
)

DEFAULT_BASELINE = Path(".agentops/baselines/hybrid-rollout-scoreboard.json")
DEFAULT_STREAM_NAME = "Custom-ClinicRecallMetrics"
_REASON_CODE_RE = re.compile(r"^[a-z0-9_.-]{1,128}$")
_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "source",
        "metrics",
        "named_regressions",
        "full_suite_failures",
        "provenance",
    }
)
_PROBE_KEYS = frozenset(
    {
        "schema_version",
        "phase",
        "scenario",
        "run_uuid",
        "passed",
        "reason_codes",
        "window",
        "fingerprints",
        "cardinalities",
        "metrics",
    }
)
_AGENTOPS_METRIC_NAMES = {
    "smoke-core": "recall_smoke_core",
    "inbound-smoke-core": "inbound_smoke_core",
    "coherence": "coherence",
    "fluency": "fluency",
}
_RED_TEAM_CATEGORIES = frozenset({"hate_unfairness", "self_harm", "violence"})


def canonical_sha256(value: object) -> str:
    """Hash canonical aggregate evidence without importing broader gate tooling."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MetricRowError(ValueError):
    """Raised when evidence cannot be represented as bounded metric rows."""


@dataclass(frozen=True)
class MetricContext:
    """Opaque CI and deployment identity stamped onto each emitted row."""

    environment: str = ""
    suite: str = ""
    workflow_name: str = ""
    workflow_run_url: str = ""
    image_tag: str = ""
    revision: str = ""
    arm: str = ""
    programme_uuid: str = ""
    git_sha: str = ""
    model_version: str = ""
    prompt_sha: str = ""
    config_sha: str = ""
    phase: int = 0


_DEFAULT_CONTEXT = MetricContext()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MetricRowError(f"{path}: invalid JSON document") from exc
    if not isinstance(value, dict):
        raise MetricRowError(f"{path}: expected a JSON object")
    return value


def _finite_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MetricRowError(f"metric {name!r} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise MetricRowError(f"metric {name!r} must be finite")
    return number


def _bounded(value: object, *, maximum: int = 256) -> str:
    return str(value or "").strip()[:maximum]


def _timestamp(value: object | None = None) -> str:
    raw = _bounded(value, maximum=64)
    if not raw:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MetricRowError("evidence timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise MetricRowError("evidence timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _unit_for(name: str) -> str:
    if name.endswith("_ms"):
        return "ms"
    if name.endswith(("_rate", "_asr", "_f1", "_validity")) or name in {
        "coherence",
        "fluency",
        "safe_clinical_boundary",
        "recall_smoke_core",
        "inbound_smoke_core",
    }:
        return "ratio"
    if name.endswith("_passed") or name.startswith("regression."):
        return "boolean"
    return "count"


def _suite_for_metric(name: str, fallback: str) -> str:
    if name.startswith("recall_"):
        return "recall"
    if name.startswith("inbound_"):
        return "inbound"
    return fallback or "combined"


def _rule_gate(
    *,
    rule: Mapping[str, Any],
    baseline_value: object,
    phase: int,
) -> tuple[str, float] | None:
    required_phase = int(rule.get("required_phase", 0))
    if phase < required_phase:
        return None

    direction = str(rule.get("direction") or "")
    closure_phase = rule.get("closure_phase")
    if closure_phase is not None and phase >= int(closure_phase):
        direction = str(rule.get("closure_direction") or direction)
        threshold = _finite_number(rule.get("closure_target"), name="closure_target")
        return {"min": ">=", "max": "<=", "equal": "=="}[direction], threshold

    baseline = (
        _finite_number(baseline_value, name="baseline_value")
        if baseline_value is not None
        else None
    )
    absolute_noise = float(rule.get("absolute_noise", 0.0))
    relative_noise = float(rule.get("relative_noise", 0.0))
    noise = 0.0 if baseline is None else absolute_noise + abs(baseline) * relative_noise

    if direction == "equal":
        target = rule.get("target", baseline)
        return "==", _finite_number(target, name="target")
    if direction == "min":
        candidates = []
        if baseline is not None:
            candidates.append(baseline - noise)
        if rule.get("hard_floor") is not None:
            candidates.append(_finite_number(rule["hard_floor"], name="hard_floor"))
        if not candidates:
            raise MetricRowError("minimum metric rule has no threshold")
        return ">=", max(candidates)
    if direction == "max":
        candidates: list[tuple[float, str]] = []
        if baseline is not None:
            candidates.append((baseline + noise, "<="))
        if rule.get("hard_ceiling") is not None:
            candidates.append(
                (
                    _finite_number(rule["hard_ceiling"], name="hard_ceiling"),
                    "<" if rule.get("exclusive_hard_ceiling") else "<=",
                )
            )
        if not candidates:
            raise MetricRowError("maximum metric rule has no threshold")
        threshold, comparator = min(candidates, key=lambda candidate: candidate[0])
        return comparator, threshold
    raise MetricRowError(f"unknown metric rule direction: {direction!r}")


def _passes(value: float, gate: tuple[str, float] | None) -> bool | None:
    if gate is None:
        return None
    comparator, threshold = gate
    return {
        ">=": value >= threshold,
        "<=": value <= threshold,
        "<": value < threshold,
        "==": value == threshold,
    }[comparator]


def _row(
    *,
    name: str,
    value: float,
    source: str,
    timestamp: str,
    phase: int,
    context: MetricContext,
    gate: tuple[str, float] | None,
    suite: str,
    eval_run_id: str = "",
    reason_codes: Sequence[str] = (),
    fingerprint_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    passed = _passes(value, gate)
    comparator, threshold = gate if gate is not None else ("", None)
    validated_reasons = []
    for reason in reason_codes:
        normalized = _bounded(reason, maximum=128)
        if not _REASON_CODE_RE.fullmatch(normalized):
            raise MetricRowError("reason codes must be bounded machine identifiers")
        validated_reasons.append(normalized)
    row = {
        "TimeGenerated": timestamp,
        "Environment": _bounded(context.environment, maximum=64),
        "Source": _bounded(source, maximum=64),
        "Suite": _bounded(suite, maximum=64),
        "WorkflowName": _bounded(context.workflow_name, maximum=128),
        "MetricName": _bounded(name, maximum=128),
        "MetricValue": value,
        "Unit": _unit_for(name),
        "Threshold": threshold,
        "Comparator": comparator,
        "Passed": passed,
        "Phase": phase,
        "Arm": _bounded(context.arm, maximum=64),
        "ProgrammeUuid": _bounded(context.programme_uuid, maximum=128),
        "GitSha": _bounded(context.git_sha, maximum=64),
        "ImageTag": _bounded(context.image_tag, maximum=128),
        "Revision": _bounded(context.revision, maximum=128),
        "ModelVersion": _bounded(context.model_version, maximum=128),
        "PromptSha": _bounded(context.prompt_sha, maximum=128),
        "ConfigSha": _bounded(context.config_sha, maximum=128),
        "EvalRunId": _bounded(eval_run_id, maximum=128),
        "WorkflowRunUrl": _bounded(context.workflow_run_url, maximum=512),
        "ReasonCode": ";".join(sorted(set(validated_reasons)))[:256],
    }
    row["EvidenceFingerprint"] = canonical_sha256(
        {
            "environment": row["Environment"],
            "source": row["Source"],
            "suite": row["Suite"],
            "metric_name": row["MetricName"],
            "metric_value": row["MetricValue"],
            "threshold": row["Threshold"],
            "comparator": row["Comparator"],
            "phase": row["Phase"],
            "git_sha": row["GitSha"],
            "prompt_sha": row["PromptSha"],
            "config_sha": row["ConfigSha"],
            "identity": dict(fingerprint_identity or {}),
        }
    )
    return row


def metric_rows_from_evidence(
    document: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    context: MetricContext = _DEFAULT_CONTEXT,
) -> list[dict[str, Any]]:
    """Convert one normalized v2 evidence document to allow-listed rows."""
    unknown = set(document) - _EVIDENCE_KEYS
    required = {"schema_version", "source", "metrics", "provenance"}
    if unknown or not required.issubset(document):
        raise MetricRowError("evidence must use the normalized aggregate-only schema")
    if document["schema_version"] != baseline.get("schema_version"):
        raise MetricRowError("evidence schema version does not match the metric baseline")
    metrics = document["metrics"]
    provenance = document["provenance"]
    if not isinstance(metrics, Mapping) or not isinstance(provenance, Mapping):
        raise MetricRowError("evidence metrics and provenance must be objects")

    metric_rules = baseline.get("metric_rules")
    baseline_values = baseline.get("baseline_values")
    if not isinstance(metric_rules, Mapping) or not isinstance(baseline_values, Mapping):
        raise MetricRowError("metric baseline is malformed")
    unknown_metrics = set(metrics) - set(metric_rules)
    if unknown_metrics:
        raise MetricRowError(
            f"evidence contains non-allow-listed metrics: {', '.join(sorted(unknown_metrics))}"
        )

    phase = int(provenance.get("phase", 0))
    if not 0 <= phase <= 12:
        raise MetricRowError("evidence phase must be between 0 and 12")
    source = _bounded(document["source"], maximum=64)
    timestamp = _timestamp(provenance.get("generated_at") or provenance.get("utc_ended_at"))
    hosted_agents = provenance.get("hosted_agents")
    hosted_agents = hosted_agents if isinstance(hosted_agents, Mapping) else {}
    merged_context = MetricContext(
        environment=context.environment or _bounded(provenance.get("environment"), maximum=64),
        suite=context.suite,
        workflow_name=context.workflow_name,
        workflow_run_url=context.workflow_run_url,
        image_tag=context.image_tag,
        revision=context.revision,
        arm=context.arm,
        programme_uuid=context.programme_uuid,
        git_sha=context.git_sha or _bounded(provenance.get("git_head"), maximum=64),
        model_version=context.model_version
        or _bounded(provenance.get("model_version"), maximum=128),
        prompt_sha=context.prompt_sha
        or _bounded(provenance.get("classifier_prompt_sha256"), maximum=128),
        config_sha=context.config_sha
        or _bounded(provenance.get("classifier_schema_sha256"), maximum=128),
    )
    fingerprint_identity = {
        "source_artifact_sha256": _bounded(
            provenance.get("source_artifact_sha256"), maximum=64
        ),
        "source_artifact_id": _bounded(provenance.get("source_artifact_id"), maximum=128),
    }

    rows: list[dict[str, Any]] = []
    for name, raw_value in sorted(metrics.items()):
        value = _finite_number(raw_value, name=name)
        rule = metric_rules[name]
        if not isinstance(rule, Mapping):
            raise MetricRowError(f"metric rule {name!r} is malformed")
        suite = _suite_for_metric(name, merged_context.suite)
        hosted = hosted_agents.get(suite)
        eval_run_id = (
            _bounded(hosted.get("eval_run_id"), maximum=128)
            if isinstance(hosted, Mapping)
            else ""
        )
        gate = _rule_gate(
            rule=rule,
            baseline_value=baseline_values.get(name),
            phase=phase,
        )
        passed = _passes(value, gate)
        rows.append(
            _row(
                name=name,
                value=value,
                source=source,
                timestamp=timestamp,
                phase=phase,
                context=merged_context,
                gate=gate,
                suite=suite,
                eval_run_id=eval_run_id,
                reason_codes=("metric_threshold_failed",) if passed is False else (),
                fingerprint_identity=fingerprint_identity,
            )
        )

    regressions = document.get("named_regressions", {})
    if not isinstance(regressions, Mapping):
        raise MetricRowError("named regressions must be an object")
    regression_rules = baseline.get("named_regressions", {})
    if not isinstance(regression_rules, Mapping) or set(regressions) - set(regression_rules):
        raise MetricRowError("evidence contains non-allow-listed named regressions")
    for name, raw_value in sorted(regressions.items()):
        if not isinstance(raw_value, bool):
            raise MetricRowError("named regression values must be booleans")
        required_phase = int(regression_rules[name].get("required_phase", 0))
        gate = ("==", 1.0) if phase >= required_phase else None
        rows.append(
            _row(
                name=f"regression.{name}",
                value=float(raw_value),
                source=source,
                timestamp=timestamp,
                phase=phase,
                context=merged_context,
                gate=gate,
                suite=merged_context.suite or "combined",
                reason_codes=("named_regression_failed",) if not raw_value else (),
                fingerprint_identity=fingerprint_identity,
            )
        )
    return rows


def metric_rows_from_probe(
    document: Mapping[str, Any],
    *,
    context: MetricContext = _DEFAULT_CONTEXT,
) -> list[dict[str, Any]]:
    """Convert one aggregate-only KQL scorecard to bounded probe rows."""
    unknown = set(document) - _PROBE_KEYS
    if unknown or document.get("schema_version") != 1:
        raise MetricRowError("probe evidence must use the KQL scorecard schema")
    if not isinstance(document.get("passed"), bool):
        raise MetricRowError("probe scorecard must have a boolean verdict")
    phase = int(document.get("phase", 0)) if "phase" in document else 0
    window = document.get("window")
    timestamp = _timestamp(window.get("end") if isinstance(window, Mapping) else None)
    scenario = _bounded(document.get("scenario"), maximum=64)
    suite = context.suite or "voice"
    reason_codes = document.get("reason_codes", [])
    if not isinstance(reason_codes, list) or any(
        not isinstance(reason, str) for reason in reason_codes
    ):
        raise MetricRowError("probe reason codes must be a list of identifiers")
    identity = {
        "run_uuid": _bounded(document.get("run_uuid"), maximum=128),
        "scenario": scenario,
    }
    rows = [
        _row(
            name="probe.scorecard_passed",
            value=float(document["passed"]),
            source="probe",
            timestamp=timestamp,
            phase=phase,
            context=context,
            gate=("==", 1.0),
            suite=suite,
            reason_codes=reason_codes,
            fingerprint_identity=identity,
        )
    ]

    fingerprints = document.get("fingerprints", {})
    cardinalities = document.get("cardinalities", {})
    metrics = document.get("metrics", {})
    if not all(isinstance(value, Mapping) for value in (fingerprints, cardinalities, metrics)):
        raise MetricRowError("probe aggregates must be objects")
    if set(fingerprints) - set(FINGERPRINT_FIELDS):
        raise MetricRowError("probe contains an unknown fingerprint")
    if set(cardinalities) - set(CARDINALITY_FIELDS):
        raise MetricRowError("probe contains an unknown cardinality")
    if set(metrics) - set(METRIC_FIELDS):
        raise MetricRowError("probe contains an unknown metric")

    for name, raw_value in sorted(fingerprints.items()):
        if not isinstance(raw_value, bool):
            raise MetricRowError("probe fingerprints must be booleans")
        rows.append(
            _row(
                name=f"probe.fingerprint.{name}",
                value=float(raw_value),
                source="probe",
                timestamp=timestamp,
                phase=phase,
                context=context,
                gate=None,
                suite=suite,
                fingerprint_identity=identity,
            )
        )
    for name, raw_value in sorted(cardinalities.items()):
        rows.append(
            _row(
                name=f"probe.cardinality.{name}",
                value=_finite_number(raw_value, name=name),
                source="probe",
                timestamp=timestamp,
                phase=phase,
                context=context,
                gate=None,
                suite=suite,
                fingerprint_identity=identity,
            )
        )
    for name, raw_value in sorted(metrics.items()):
        if raw_value is None:
            continue
        gate = {
            "severity_3_or_higher": ("==", 0.0),
            "semantic_p95_ms": ("<=", 600.0),
            "active_tasks_max_per_anchor_kind": ("==", 1.0) if phase >= 5 else None,
        }[name]
        value = _finite_number(raw_value, name=name)
        rows.append(
            _row(
                name=f"probe.{name}",
                value=value,
                source="probe",
                timestamp=timestamp,
                phase=phase,
                context=context,
                gate=gate,
                suite=suite,
                reason_codes=("probe_metric_threshold_failed",)
                if _passes(value, gate) is False
                else (),
                fingerprint_identity=identity,
            )
        )
    return rows


def _target_suite(target: object, fallback: str) -> str:
    if isinstance(target, Mapping):
        target = target.get("raw") or target.get("name")
    normalized = _bounded(target, maximum=128).lower()
    if normalized.startswith("recall-agent"):
        return "recall"
    if normalized.startswith("inbound-assistant"):
        return "inbound"
    return fallback or "combined"


def _raw_identity(source: str, payload: Mapping[str, Any]) -> dict[str, str]:
    return {"raw_summary_sha256": canonical_sha256({"source": source, **payload})}


def metric_rows_from_agentops_result(
    document: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    context: MetricContext = _DEFAULT_CONTEXT,
) -> list[dict[str, Any]]:
    """Extract only aggregate scores from an AgentOps results.json document."""
    aggregate = document.get("aggregate_metrics")
    if not isinstance(aggregate, Mapping):
        raise MetricRowError("AgentOps result is missing aggregate_metrics")
    selected = {
        canonical_name: aggregate[source_name]
        for source_name, canonical_name in _AGENTOPS_METRIC_NAMES.items()
        if source_name in aggregate
    }
    if not selected:
        raise MetricRowError("AgentOps result contains no allow-listed aggregate metrics")
    metric_rules = baseline.get("metric_rules")
    baseline_values = baseline.get("baseline_values")
    if not isinstance(metric_rules, Mapping) or not isinstance(baseline_values, Mapping):
        raise MetricRowError("metric baseline is malformed")

    suite = _target_suite(document.get("target"), context.suite)
    config = document.get("config")
    config = config if isinstance(config, Mapping) else {}
    azd = config.get("azd_evaluation")
    azd = azd if isinstance(azd, Mapping) else {}
    timestamp = _timestamp(document.get("finished_at"))
    identity = _raw_identity(
        "agentops",
        {
            "target": _bounded(document.get("target"), maximum=128),
            "metrics": selected,
            "eval_run_id": _bounded(azd.get("run_id"), maximum=128),
        },
    )
    rows = []
    for name, raw_value in sorted(selected.items()):
        value = _finite_number(raw_value, name=name)
        rule = metric_rules.get(name)
        if not isinstance(rule, Mapping):
            raise MetricRowError(f"metric rule {name!r} is missing")
        gate = _rule_gate(
            rule=rule,
            baseline_value=baseline_values.get(name),
            phase=context.phase,
        )
        rows.append(
            _row(
                name=name,
                value=value,
                source="agentops",
                timestamp=timestamp,
                phase=context.phase,
                context=context,
                gate=gate,
                suite=_suite_for_metric(name, suite),
                eval_run_id=_bounded(azd.get("run_id"), maximum=128),
                reason_codes=("metric_threshold_failed",)
                if _passes(value, gate) is False
                else (),
                fingerprint_identity=identity,
            )
        )
    duration = document.get("duration_seconds")
    if duration is not None:
        rows.append(
            _row(
                name="agentops.duration_seconds",
                value=_finite_number(duration, name="duration_seconds"),
                source="agentops",
                timestamp=timestamp,
                phase=context.phase,
                context=context,
                gate=None,
                suite=suite,
                eval_run_id=_bounded(azd.get("run_id"), maximum=128),
                fingerprint_identity=identity,
            )
        )
    return rows


def metric_rows_from_assert_summary(
    document: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    context: MetricContext = _DEFAULT_CONTEXT,
) -> list[dict[str, Any]]:
    """Extract violation and execution aggregates from ASSERT latest.json."""
    suite = _target_suite(document.get("suite"), context.suite)
    metric_name = {
        "recall": "recall_assert_violations",
        "inbound": "inbound_assert_violations",
    }.get(suite)
    if metric_name is None:
        raise MetricRowError("ASSERT summary suite must identify recall or inbound")
    failed = _finite_number(document.get("failed_cases"), name="failed_cases")
    total = _finite_number(document.get("total_cases"), name="total_cases")
    skipped = _finite_number(document.get("skipped_cases", 0), name="skipped_cases")
    raw_pass_rate = document.get("pass_rate")
    pass_rate = (
        None
        if raw_pass_rate is None
        else _finite_number(raw_pass_rate, name="pass_rate")
    )
    rule = baseline.get("metric_rules", {}).get(metric_name)
    if not isinstance(rule, Mapping):
        raise MetricRowError(f"metric rule {metric_name!r} is missing")
    gate = _rule_gate(
        rule=rule,
        baseline_value=baseline.get("baseline_values", {}).get(metric_name),
        phase=context.phase,
    )
    identity = _raw_identity(
        "assert",
        {
            "suite": suite,
            "run_id": _bounded(document.get("run_id"), maximum=128),
            "failed": failed,
            "total": total,
            "skipped": skipped,
        },
    )
    timestamp = _timestamp(document.get("generated_at"))
    definitions = [
        (metric_name, failed, gate),
        ("assert.total_cases", total, None),
        ("assert.skipped_cases", skipped, None),
    ]
    if pass_rate is not None:
        definitions.insert(1, ("assert.pass_rate", pass_rate, ("==", 1.0)))
    return [
        _row(
            name=name,
            value=value,
            source="assert",
            timestamp=timestamp,
            phase=context.phase,
            context=context,
            gate=row_gate,
            suite=suite,
            reason_codes=("assert_gate_failed",)
            if _passes(value, row_gate) is False
            else (),
            fingerprint_identity=identity,
        )
        for name, value, row_gate in definitions
    ]


def metric_rows_from_red_team_summary(
    document: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    context: MetricContext = _DEFAULT_CONTEXT,
) -> list[dict[str, Any]]:
    """Extract ASR and bounded category aggregates from Red Team latest.json."""
    suite = context.suite or "recall"
    metric_name = {
        "recall": "recall_red_team_asr",
        "inbound": "inbound_red_team_asr",
    }.get(suite)
    if metric_name is None:
        raise MetricRowError("Red Team summary requires --suite recall or inbound")
    asr = _finite_number(document.get("attack_success_rate"), name="attack_success_rate")
    attempts = _finite_number(document.get("total_attempts"), name="total_attempts")
    successful = _finite_number(
        document.get("successful_attacks"), name="successful_attacks"
    )
    rule = baseline.get("metric_rules", {}).get(metric_name)
    if not isinstance(rule, Mapping):
        raise MetricRowError(f"metric rule {metric_name!r} is missing")
    gate = _rule_gate(
        rule=rule,
        baseline_value=baseline.get("baseline_values", {}).get(metric_name),
        phase=context.phase,
    )
    identity = _raw_identity(
        "red_team",
        {"suite": suite, "asr": asr, "attempts": attempts, "successful": successful},
    )
    timestamp = _timestamp(document.get("generated_at"))
    rows = [
        _row(
            name=metric_name,
            value=asr,
            source="red_team",
            timestamp=timestamp,
            phase=context.phase,
            context=context,
            gate=gate,
            suite=suite,
            reason_codes=("red_team_gate_failed",) if _passes(asr, gate) is False else (),
            fingerprint_identity=identity,
        ),
        _row(
            name="redteam.total_attempts",
            value=attempts,
            source="red_team",
            timestamp=timestamp,
            phase=context.phase,
            context=context,
            gate=None,
            suite=suite,
            fingerprint_identity=identity,
        ),
        _row(
            name="redteam.successful_attacks",
            value=successful,
            source="red_team",
            timestamp=timestamp,
            phase=context.phase,
            context=context,
            gate=None,
            suite=suite,
            fingerprint_identity=identity,
        ),
    ]
    categories = document.get("per_category", {})
    if not isinstance(categories, Mapping) or set(categories) - _RED_TEAM_CATEGORIES:
        raise MetricRowError("Red Team summary contains a non-allow-listed risk category")
    for category, summary in sorted(categories.items()):
        if not isinstance(summary, Mapping):
            raise MetricRowError("Red Team category summary must be an object")
        category_asr = _finite_number(
            summary.get("attack_success_rate"), name=f"{category}.attack_success_rate"
        )
        rows.append(
            _row(
                name=f"redteam.category.{category}.asr",
                value=category_asr,
                source="red_team",
                timestamp=timestamp,
                phase=context.phase,
                context=context,
                gate=("<", 0.2),
                suite=suite,
                reason_codes=("red_team_category_gate_failed",)
                if category_asr >= 0.2
                else (),
                fingerprint_identity=identity,
            )
        )
    return rows


def metric_rows_from_doctor_summary(
    document: Mapping[str, Any],
    *,
    context: MetricContext = _DEFAULT_CONTEXT,
) -> list[dict[str, Any]]:
    """Extract Doctor posture counts without copying finding or warning prose."""
    blockers = document.get("blockers", [])
    warnings = document.get("warnings", [])
    ready = document.get("ready", [])
    if not all(isinstance(value, list) for value in (blockers, warnings, ready)):
        raise MetricRowError("Doctor posture lists are malformed")
    status = _bounded(document.get("status"), maximum=64)
    ready_status = status in {"ready", "ready_with_warnings"}
    suite = _target_suite(document.get("target"), context.suite)
    timestamp = _timestamp(document.get("generated_at"))
    identity = _raw_identity(
        "doctor",
        {
            "status": status,
            "blocker_count": len(blockers),
            "warning_count": len(warnings),
            "ready_count": len(ready),
        },
    )
    definitions = (
        ("doctor.ready", float(ready_status), ("==", 1.0)),
        ("doctor.blocker_count", float(len(blockers)), ("==", 0.0)),
        ("doctor.warning_count", float(len(warnings)), None),
        ("doctor.ready_check_count", float(len(ready)), None),
    )
    return [
        _row(
            name=name,
            value=value,
            source="doctor",
            timestamp=timestamp,
            phase=context.phase,
            context=context,
            gate=gate,
            suite=suite,
            reason_codes=("doctor_posture_blocked",)
            if _passes(value, gate) is False
            else (),
            fingerprint_identity=identity,
        )
        for name, value, gate in definitions
    ]


def upload_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    endpoint: str,
    rule_id: str,
    stream_name: str,
) -> None:
    """Upload rows with DefaultAzureCredential and the Logs Ingestion SDK."""
    try:
        from azure.identity import DefaultAzureCredential
        from azure.monitor.ingestion import LogsIngestionClient
    except ImportError as exc:
        raise MetricRowError(
            "upload requires azure-identity and azure-monitor-ingestion"
        ) from exc
    credential = DefaultAzureCredential()
    client = LogsIngestionClient(endpoint=endpoint, credential=credential)
    try:
        client.upload(rule_id=rule_id, stream_name=stream_name, logs=list(rows))
    finally:
        client.close()
        credential.close()


def _context_from_args(args: argparse.Namespace) -> MetricContext:
    return MetricContext(
        environment=args.environment,
        suite=args.suite,
        workflow_name=args.workflow_name,
        workflow_run_url=args.workflow_run_url,
        image_tag=args.image_tag,
        revision=args.revision,
        arm=args.arm,
        programme_uuid=args.programme_uuid,
        git_sha=args.git_sha,
        model_version=args.model_version,
        prompt_sha=args.prompt_sha,
        config_sha=args.config_sha,
        phase=args.phase,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", action="append", type=Path, default=[])
    parser.add_argument("--probe", action="append", type=Path, default=[])
    parser.add_argument("--agentops-result", action="append", type=Path, default=[])
    parser.add_argument("--assert-result", action="append", type=Path, default=[])
    parser.add_argument("--red-team-result", action="append", type=Path, default=[])
    parser.add_argument("--doctor-result", action="append", type=Path, default=[])
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--jsonl-out", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--endpoint", default=os.getenv("AZURE_MONITOR_INGESTION_ENDPOINT", "")
    )
    parser.add_argument("--rule-id", default=os.getenv("AZURE_MONITOR_DCR_RULE_ID", ""))
    parser.add_argument(
        "--stream-name",
        default=os.getenv("AZURE_MONITOR_STREAM_NAME", DEFAULT_STREAM_NAME),
    )
    parser.add_argument("--environment", default=os.getenv("DEPLOYMENT_ENVIRONMENT", ""))
    parser.add_argument("--suite", default=os.getenv("METRIC_SUITE", ""))
    parser.add_argument("--workflow-name", default=os.getenv("GITHUB_WORKFLOW", ""))
    parser.add_argument(
        "--workflow-run-url",
        default=(
            f"{os.getenv('GITHUB_SERVER_URL', '')}/{os.getenv('GITHUB_REPOSITORY', '')}"
            f"/actions/runs/{os.getenv('GITHUB_RUN_ID', '')}"
            if os.getenv("GITHUB_RUN_ID")
            else ""
        ),
    )
    parser.add_argument("--image-tag", default=os.getenv("IMAGE_TAG", ""))
    parser.add_argument("--revision", default=os.getenv("CONTAINER_APP_REVISION", ""))
    parser.add_argument("--arm", default=os.getenv("EXPERIMENT_ARM", ""))
    parser.add_argument("--programme-uuid", default=os.getenv("PROGRAMME_UUID", ""))
    parser.add_argument("--git-sha", default=os.getenv("GITHUB_SHA", ""))
    parser.add_argument("--model-version", default=os.getenv("MODEL_VERSION", ""))
    parser.add_argument("--prompt-sha", default=os.getenv("PROMPT_SHA", ""))
    parser.add_argument("--config-sha", default=os.getenv("CONFIG_SHA", ""))
    parser.add_argument("--phase", type=int, default=int(os.getenv("ROLLOUT_PHASE", "0")))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not any(
        (
            args.evidence,
            args.probe,
            args.agentops_result,
            args.assert_result,
            args.red_team_result,
            args.doctor_result,
        )
    ):
        raise SystemExit("at least one metric evidence input is required")
    baseline = _read_json(args.baseline)
    context = _context_from_args(args)
    rows: list[dict[str, Any]] = []
    for path in args.evidence:
        rows.extend(metric_rows_from_evidence(_read_json(path), baseline=baseline, context=context))
    for path in args.probe:
        rows.extend(metric_rows_from_probe(_read_json(path), context=context))
    for path in args.agentops_result:
        rows.extend(
            metric_rows_from_agentops_result(
                _read_json(path), baseline=baseline, context=context
            )
        )
    for path in args.assert_result:
        rows.extend(
            metric_rows_from_assert_summary(
                _read_json(path), baseline=baseline, context=context
            )
        )
    for path in args.red_team_result:
        rows.extend(
            metric_rows_from_red_team_summary(
                _read_json(path), baseline=baseline, context=context
            )
        )
    for path in args.doctor_result:
        rows.extend(metric_rows_from_doctor_summary(_read_json(path), context=context))
    if not rows:
        raise SystemExit("no metric rows were emitted")

    encoded = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    if args.jsonl_out:
        args.jsonl_out.parent.mkdir(parents=True, exist_ok=True)
        args.jsonl_out.write_text(encoded, encoding="utf-8")
    if args.dry_run:
        print(encoded, end="")
        return 0
    if not args.endpoint or not args.rule_id:
        raise SystemExit("upload requires --endpoint and --rule-id")
    upload_rows(
        rows,
        endpoint=args.endpoint,
        rule_id=args.rule_id,
        stream_name=args.stream_name,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())