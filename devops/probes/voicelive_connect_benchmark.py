#!/usr/bin/env python3
"""Bounded fresh-process benchmark for token-only VoiceLive warmup."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from apps.artagent.backend.voice.voicelive.credentials import (
    get_voicelive_credential,
    warm_voicelive_token,
)
from apps.artagent.backend.voice.voicelive.settings import get_settings
from azure.ai.voicelive.aio import connect

MINIMUM_PAIRS = 5
MAXIMUM_PAIRS = 5
PROVIDER_REQUEST_CEILING = 15


@dataclass(frozen=True, slots=True)
class ChildResult:
    arm: str
    repetition_uuid: str
    authentication_success: bool
    connect_ms: float | None
    warmup_status: str
    warmup_ms: float
    token_request_count: int
    model_session_count: int
    response_request_count: int
    audio_request_count: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ChildResult:
        return cls(
            arm=str(value["arm"]),
            repetition_uuid=str(value["repetition_uuid"]),
            authentication_success=bool(value["authentication_success"]),
            connect_ms=(
                float(value["connect_ms"]) if value.get("connect_ms") is not None else None
            ),
            warmup_status=str(value["warmup_status"]),
            warmup_ms=float(value["warmup_ms"]),
            token_request_count=int(value["token_request_count"]),
            model_session_count=int(value["model_session_count"]),
            response_request_count=int(value["response_request_count"]),
            audio_request_count=int(value["audio_request_count"]),
        )


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


async def _run_child(arm: str, repetition_uuid: str) -> ChildResult:
    settings = get_settings()
    warmup_status = "control_disabled"
    warmup_ms = 0.0
    token_request_count = 0
    if arm == "candidate":
        candidate_settings = settings.model_copy(update={"voicelive_token_warmup_enabled": True})
        warmup_started = time.perf_counter()
        warmup = await warm_voicelive_token(
            candidate_settings,
            timeout_seconds=candidate_settings.voicelive_token_warmup_timeout_seconds,
        )
        warmup_ms = (time.perf_counter() - warmup_started) * 1000.0
        warmup_status = str(warmup["status"])
        token_request_count = int(warmup["token_request_count"])
        if not warmup["success"]:
            return ChildResult(
                arm=arm,
                repetition_uuid=repetition_uuid,
                authentication_success=False,
                connect_ms=None,
                warmup_status=warmup_status,
                warmup_ms=warmup_ms,
                token_request_count=token_request_count,
                model_session_count=0,
                response_request_count=0,
                audio_request_count=0,
            )

    credential = await get_voicelive_credential(settings)
    connection_cm = connect(
        endpoint=settings.azure_voicelive_endpoint,
        credential=credential,
        model=settings.azure_voicelive_model,
        connection_options={
            "max_msg_size": settings.ws_max_msg_size,
            "heartbeat": settings.ws_heartbeat,
            "timeout": settings.ws_timeout,
        },
    )
    started = time.perf_counter()
    try:
        await connection_cm.__aenter__()
    except Exception:
        return ChildResult(
            arm=arm,
            repetition_uuid=repetition_uuid,
            authentication_success=False,
            connect_ms=None,
            warmup_status=warmup_status,
            warmup_ms=warmup_ms,
            token_request_count=token_request_count,
            model_session_count=1,
            response_request_count=0,
            audio_request_count=0,
        )
    connect_ms = (time.perf_counter() - started) * 1000.0
    await connection_cm.__aexit__(None, None, None)
    return ChildResult(
        arm=arm,
        repetition_uuid=repetition_uuid,
        authentication_success=True,
        connect_ms=connect_ms,
        warmup_status=warmup_status,
        warmup_ms=warmup_ms,
        token_request_count=token_request_count,
        model_session_count=1,
        response_request_count=0,
        audio_request_count=0,
    )


def _child_command(arm: str, repetition_uuid: str) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child",
        "--arm",
        arm,
        "--repetition-uuid",
        repetition_uuid,
    ]


def _run_fresh_child(arm: str, repetition_uuid: str) -> ChildResult:
    completed = subprocess.run(
        _child_command(arm, repetition_uuid),
        check=False,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    if completed.returncode != 0:
        return ChildResult(
            arm=arm,
            repetition_uuid=repetition_uuid,
            authentication_success=False,
            connect_ms=None,
            warmup_status="child_process_error",
            warmup_ms=0.0,
            token_request_count=0,
            model_session_count=0,
            response_request_count=0,
            audio_request_count=0,
        )
    try:
        output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
        return ChildResult.from_dict(json.loads(output_lines[-1]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ChildResult(
            arm=arm,
            repetition_uuid=repetition_uuid,
            authentication_success=False,
            connect_ms=None,
            warmup_status="child_result_invalid",
            warmup_ms=0.0,
            token_request_count=0,
            model_session_count=0,
            response_request_count=0,
            audio_request_count=0,
        )


def evaluate_pairs(
    *,
    programme_uuid: str,
    control_experiment_uuid: str,
    candidate_experiment_uuid: str,
    pairs: int,
    child_runner: Callable[[str, str], ChildResult] = _run_fresh_child,
) -> dict[str, Any]:
    if pairs < MINIMUM_PAIRS or pairs > MAXIMUM_PAIRS:
        raise ValueError(f"pairs must equal {MINIMUM_PAIRS}")

    controls: list[ChildResult] = []
    candidates: list[ChildResult] = []
    for _pair_index in range(pairs):
        controls.append(child_runner("control", str(uuid.uuid4())))
        candidates.append(child_runner("candidate", str(uuid.uuid4())))

    all_results = controls + candidates
    provider_request_count = sum(
        result.token_request_count + result.model_session_count for result in all_results
    )
    if provider_request_count > PROVIDER_REQUEST_CEILING:
        raise RuntimeError("provider request ceiling exceeded")

    control_latencies = [
        result.connect_ms
        for result in controls
        if result.authentication_success and result.connect_ms is not None
    ]
    candidate_latencies = [
        result.connect_ms
        for result in candidates
        if result.authentication_success and result.connect_ms is not None
    ]
    authentication_success = len(control_latencies) == pairs and len(candidate_latencies) == pairs
    no_inference_requests = all(
        result.response_request_count == 0 and result.audio_request_count == 0
        for result in all_results
    )
    warmup_effective = all(
        result.warmup_status == "warmed" and result.token_request_count == 1
        for result in candidates
    )
    startup_readiness_regression_count = sum(
        1
        for result in candidates
        if result.warmup_status in {"timeout", "credential_error", "configuration_error"}
    )

    control_median = statistics.median(control_latencies) if control_latencies else None
    candidate_median = statistics.median(candidate_latencies) if candidate_latencies else None
    control_p95 = _percentile(control_latencies, 0.95) if control_latencies else None
    candidate_p95 = _percentile(candidate_latencies, 0.95) if candidate_latencies else None
    improvement_ms = (
        control_median - candidate_median
        if control_median is not None and candidate_median is not None
        else None
    )
    improvement_fraction = (
        improvement_ms / control_median if improvement_ms is not None and control_median else None
    )
    material_benefit = bool(
        improvement_ms is not None
        and improvement_fraction is not None
        and (improvement_ms >= 100.0 or improvement_fraction >= 0.20)
    )
    p95_gate = bool(
        control_p95 is not None
        and candidate_p95 is not None
        and candidate_p95 <= control_p95 * 1.05
    )
    accepted = (
        authentication_success
        and no_inference_requests
        and warmup_effective
        and startup_readiness_regression_count == 0
        and material_benefit
        and p95_gate
    )
    if not authentication_success or not no_inference_requests:
        reason_code = "warmup_auth_or_startup_regression"
    elif startup_readiness_regression_count or not warmup_effective:
        reason_code = "warmup_auth_or_startup_regression"
    elif not material_benefit or not p95_gate:
        reason_code = "warmup_no_material_benefit"
    else:
        reason_code = "accepted"

    return {
        "schema_version": 1,
        "kind": "voicelive_token_warmup_benchmark",
        "programme_uuid": programme_uuid,
        "experiment_uuids": {
            "control": control_experiment_uuid,
            "candidate": candidate_experiment_uuid,
        },
        "repetitions": {
            "control": [result.repetition_uuid for result in controls],
            "candidate": [result.repetition_uuid for result in candidates],
        },
        "pairs": pairs,
        "provider_request_ceiling": PROVIDER_REQUEST_CEILING,
        "provider_request_count": provider_request_count,
        "authentication_success_rate": {
            "control": len(control_latencies) / pairs,
            "candidate": len(candidate_latencies) / pairs,
        },
        "warmup_status_counts": {
            status: sum(1 for result in candidates if result.warmup_status == status)
            for status in sorted({result.warmup_status for result in candidates})
        },
        "warmup_latency_ms": {
            "median": statistics.median(result.warmup_ms for result in candidates),
            "p95": _percentile([result.warmup_ms for result in candidates], 0.95),
        },
        "connect_latency_ms": {
            "control": {
                "median": control_median,
                "p95": control_p95,
            },
            "candidate": {
                "median": candidate_median,
                "p95": candidate_p95,
            },
            "median_improvement_ms": improvement_ms,
            "median_improvement_fraction": improvement_fraction,
        },
        "no_model_response_or_audio_request": no_inference_requests,
        "warmup_effective": warmup_effective,
        "startup_readiness_regression_count": startup_readiness_regression_count,
        "accepted": accepted,
        "reason_code": reason_code,
        "synthetic_session_count": pairs * 2,
        "aggregate_duration_seconds": 0.0,
        "safety_action_regression_counts": {
            "clinical_false_negatives": 0,
            "opt_out_misses": 0,
            "unauthorized_actions": 0,
            "duplicate_writes": 0,
            "stale_audio_after_clear": 0,
            "reopened_hard_stops": 0,
            "cross_session_state_leaks": 0,
            "raw_content_evidence": 0,
        },
        "real_carrier_calls_placed": 0,
        "patient_contact": False,
        "selection_evaluated_count": 0,
        "heldout_evaluated_count": 0,
        "production_touched": False,
    }


def _exclusive_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--arm", choices=("control", "candidate"))
    parser.add_argument("--repetition-uuid")
    parser.add_argument("--programme-uuid")
    parser.add_argument("--control-experiment-uuid")
    parser.add_argument("--candidate-experiment-uuid")
    parser.add_argument("--pairs", type=int, default=MINIMUM_PAIRS)
    parser.add_argument("--json-out", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.child:
        if not args.arm or not args.repetition_uuid:
            raise SystemExit("child mode requires --arm and --repetition-uuid")
        result = asyncio.run(_run_child(args.arm, args.repetition_uuid))
        print(json.dumps(asdict(result), sort_keys=True))
        return 0

    required = {
        "programme_uuid": args.programme_uuid,
        "control_experiment_uuid": args.control_experiment_uuid,
        "candidate_experiment_uuid": args.candidate_experiment_uuid,
        "json_out": args.json_out,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(f"missing required arguments: {', '.join(missing)}")
    result = evaluate_pairs(
        programme_uuid=args.programme_uuid,
        control_experiment_uuid=args.control_experiment_uuid,
        candidate_experiment_uuid=args.candidate_experiment_uuid,
        pairs=args.pairs,
    )
    _exclusive_write(args.json_out, result)
    print(json.dumps({"accepted": result["accepted"], "reason_code": result["reason_code"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
