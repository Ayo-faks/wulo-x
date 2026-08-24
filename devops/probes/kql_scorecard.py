#!/usr/bin/env python3
"""Evaluate a probe window from aggregate-only staging AppTraces evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

STAGING_WORKSPACE_ID = "eb901632-087a-4cbe-91cd-a2085c6bd684"
FINGERPRINT_FIELDS = (
    "twilio_connected",
    "mixed_capture",
    "availability_route",
    "goodbye_cancelled",
    "clinic_hours_route",
    "hard_stop_forward_blocked",
    "urgent_signpost_created",
    "mark_rest_termination",
    "prior_playout",
    "governed_echo_suppressed",
    "genuine_barge_clear",
    "instruction_override_routed",
    "idempotent_replay",
    "opt_out_recorded",
)
CARDINALITY_FIELDS = (
    "assistant_count",
    "clear_count",
    "escalation_count",
    "booking_count",
    "safety_route_count",
    "shadow_disagreement_count",
)
METRIC_FIELDS = (
    "severity_3_or_higher",
    "semantic_p95_ms",
    "active_tasks_max_per_anchor_kind",
)
SUPPORTING_EVIDENCE_FIELDS = (
    "mark_playout_received",
    "rest_termination",
)
AGGREGATE_FIELDS = frozenset(
    FINGERPRINT_FIELDS + CARDINALITY_FIELDS + METRIC_FIELDS + SUPPORTING_EVIDENCE_FIELDS
)
REQUIRED_FINGERPRINTS = {
    "greeting_smoke": ("twilio_connected",),
    "four_turn_replay": ("mixed_capture", "availability_route"),
    "interruptible_close": ("goodbye_cancelled", "clinic_hours_route"),
    "urgent_hard_stop": ("hard_stop_forward_blocked", "mark_rest_termination"),
    "ordered_continuation": ("prior_playout",),
    "governed_echo": ("governed_echo_suppressed",),
    "mixed_clinical_booking": ("mixed_capture",),
    "acknowledgement_bank": (),
    "genuine_barge_in": ("genuine_barge_clear",),
    "semantic_fault": ("instruction_override_routed",),
    "duplicate_storm": ("idempotent_replay",),
    "opt_out": (
        "urgent_signpost_created",
    ),
}


class ScorecardError(RuntimeError):
    """Controlled scorecard input or query failure."""


def _normalize_aggregate_value(value: Any) -> int | float | None:
    if value is None or value in {"", "None", "null"}:
        return None
    if isinstance(value, bool):
        raise ScorecardError("aggregate_value_invalid")
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        raise ScorecardError("aggregate_value_invalid")
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError as exc:
            raise ScorecardError("aggregate_value_invalid") from exc


def _validated_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScorecardError("invalid_time_window") from exc
    if parsed.tzinfo is None:
        raise ScorecardError("time_window_requires_timezone")
    return parsed.isoformat()


def build_aggregate_query(*, start: str, end: str) -> str:
    """Build a query that returns no trace message or identifier columns."""
    safe_start = _validated_timestamp(start)
    safe_end = _validated_timestamp(end)
    return f"""AppTraces
| where TimeGenerated between (datetime({safe_start}) .. datetime({safe_end}))
| extend dimensions = todynamic(Properties)
| summarize
    twilio_connected=countif(Message has "[Twilio] WebSocket connect"),
    mixed_capture=countif(Message has "mixed safety+booking request captured"),
    availability_route=countif(Message has "affirmative availability"),
    goodbye_cancelled=countif(Message has "Interruptible call-end cancelled"),
    clinic_hours_route=countif(Message has "deterministic hours completed"),
    hard_stop_forward_blocked=countif(Message has "Caller turn not forwarded during hard-stop close"),
    urgent_signpost_created=countif(Message has "Clinic Recall safety response created" and Message has "intent=urgent"),
    mark_playout_received=countif(Message has "Call-end playout mark received"),
    rest_termination=countif(Message has "REST hangup" or Message has "Call completed via REST"),
    prior_playout=countif(Message has "Prior governed playout preserved"),
    governed_echo_suppressed=countif(Message has "Governed speech echo suppressed"),
    genuine_barge_clear=countif(Message has "Barge-in clear sent"),
    instruction_override_routed=countif(Message has "instruction override routed to safety"),
    idempotent_replay=countif(Message has "idempotent=true"),
    opt_out_recorded=countif(Message has "opt-out" and Message has "success=true"),
    assistant_count=countif(Message startswith "[Twilio] Assistant:"),
    clear_count=countif(Message has "Twilio clear" or Message has "clear sent"),
    escalation_count=countif(Message has "safety routed to staff"),
    booking_count=countif(Message has "booking request captured"),
    safety_route_count=countif(Message has "safety routed"),
    shadow_disagreement_count=countif(Message has "hybrid shadow delta"),
    severity_3_or_higher=countif(SeverityLevel >= 3),
    semantic_p95_ms=percentile(todouble(dimensions.semantic_latency_ms), 95),
    active_tasks_max_per_anchor_kind=max(toint(dimensions.active_tasks_per_anchor_kind))
"""


def fetch_aggregate(
    *,
    workspace: str,
    start: str,
    end: str,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    query = build_aggregate_query(start=start, end=end)
    command = [
        "az",
        "monitor",
        "log-analytics",
        "query",
        "-w",
        workspace,
        "--analytics-query",
        query,
        "-o",
        "json",
    ]
    try:
        completed = run(command, check=True, capture_output=True, text=True)
        rows = json.loads(completed.stdout)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ScorecardError("aggregate_query_failed") from exc
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ScorecardError("aggregate_query_shape_invalid")
    return {
        key: _normalize_aggregate_value(value)
        for key, value in rows[0].items()
        if key in AGGREGATE_FIELDS
    }


def _integer(aggregate: Mapping[str, Any], key: str) -> int:
    value = aggregate.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScorecardError("aggregate_value_invalid")
    return int(value)


def _number_or_none(aggregate: Mapping[str, Any], key: str) -> float | None:
    value = aggregate.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScorecardError("aggregate_value_invalid")
    return float(value)


def evaluate_scorecard(
    verdict: Mapping[str, Any], aggregate: Mapping[str, Any], *, phase: int = 0
) -> dict[str, Any]:
    """Merge a local verdict and aggregate window into text-free evidence."""
    scenario = verdict.get("scenario")
    if scenario not in REQUIRED_FINGERPRINTS:
        raise ScorecardError("unknown_scenario")
    required = set(REQUIRED_FINGERPRINTS[scenario])
    fingerprints: dict[str, bool] = {}
    for name in FINGERPRINT_FIELDS:
        if name == "mark_rest_termination":
            if name in required:
                fingerprints[name] = (
                    _integer(aggregate, "mark_playout_received") > 0
                    and _integer(aggregate, "rest_termination") > 0
                )
        elif name in required or aggregate.get(name) is not None:
            fingerprints[name] = _integer(aggregate, name) > 0
    cardinalities = {name: _integer(aggregate, name) for name in CARDINALITY_FIELDS}
    severity = _integer(aggregate, "severity_3_or_higher")
    semantic_p95 = _number_or_none(aggregate, "semantic_p95_ms")
    active_tasks = _number_or_none(aggregate, "active_tasks_max_per_anchor_kind")

    reasons: list[str] = []
    if verdict.get("passed") is not True:
        reasons.append("local_probe_failed")
    for name in sorted(required):
        if not fingerprints.get(name, False):
            reasons.append(f"missing_fingerprint_{name}")
    if severity:
        reasons.append("severity_3_or_higher")
    if semantic_p95 is not None and semantic_p95 > 600.0:
        reasons.append("semantic_p95_above_600_ms")
    if phase >= 5 and active_tasks != 1.0:
        reasons.append("active_task_cardinality_not_one")
    if scenario == "acknowledgement_bank" and (
        cardinalities["escalation_count"] or cardinalities["booking_count"]
    ):
        reasons.append("acknowledgement_created_task")
    if scenario == "duplicate_storm" and cardinalities["booking_count"] != 1:
        reasons.append("duplicate_booking_count_not_one")

    reason_codes = sorted(set(reasons))
    return {
        "schema_version": 1,
        "scenario": scenario,
        "run_uuid": verdict.get("run_uuid"),
        "passed": not reason_codes,
        "reason_codes": reason_codes,
        "window": {
            "start": verdict.get("started_at"),
            "end": verdict.get("ended_at"),
        },
        "fingerprints": fingerprints,
        "cardinalities": cardinalities,
        "metrics": {
            "severity_3_or_higher": severity,
            "semantic_p95_ms": semantic_p95,
            "active_tasks_max_per_anchor_kind": active_tasks,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verdict", type=Path, required=True)
    parser.add_argument("--workspace", default=STAGING_WORKSPACE_ID)
    parser.add_argument("--phase", type=int, default=0)
    parser.add_argument("--json-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verdict = json.loads(args.verdict.read_text(encoding="utf-8"))
        aggregate = fetch_aggregate(
            workspace=args.workspace,
            start=verdict["started_at"],
            end=verdict["ended_at"],
        )
        scorecard = evaluate_scorecard(verdict, aggregate, phase=args.phase)
    except (OSError, KeyError, json.JSONDecodeError, ScorecardError):
        scorecard = {
            "schema_version": 1,
            "passed": False,
            "reason_codes": ["scorecard_input_or_query_failed"],
        }
    encoded = json.dumps(scorecard, sort_keys=True, separators=(",", ":"))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if scorecard["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
