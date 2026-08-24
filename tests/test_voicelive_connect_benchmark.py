import json
from dataclasses import asdict
from pathlib import Path

import pytest
from devops.probes.voicelive_connect_benchmark import (
    ChildResult,
    _exclusive_write,
    evaluate_pairs,
)


def _runner(
    control_ms: list[float],
    candidate_ms: list[float],
):
    indexes = {"control": 0, "candidate": 0}

    def run(arm: str, repetition_uuid: str) -> ChildResult:
        values = control_ms if arm == "control" else candidate_ms
        connect_ms = values[indexes[arm]]
        indexes[arm] += 1
        return ChildResult(
            arm=arm,
            repetition_uuid=repetition_uuid,
            authentication_success=True,
            connect_ms=connect_ms,
            warmup_status="control_disabled" if arm == "control" else "warmed",
            warmup_ms=0.0 if arm == "control" else 100.0,
            token_request_count=0 if arm == "control" else 1,
            model_session_count=1,
            response_request_count=0,
            audio_request_count=0,
        )

    return run


def test_warmup_benchmark_accepts_material_benefit() -> None:
    result = evaluate_pairs(
        programme_uuid="programme",
        control_experiment_uuid="control",
        candidate_experiment_uuid="candidate",
        pairs=5,
        child_runner=_runner(
            [500.0, 510.0, 520.0, 530.0, 540.0],
            [300.0, 310.0, 320.0, 330.0, 340.0],
        ),
    )

    assert result["accepted"] is True
    assert result["reason_code"] == "accepted"
    assert result["provider_request_count"] == 15
    assert result["no_model_response_or_audio_request"] is True
    assert len(set(result["repetitions"]["control"] + result["repetitions"]["candidate"])) == 10


def test_warmup_benchmark_rejects_small_benefit() -> None:
    result = evaluate_pairs(
        programme_uuid="programme",
        control_experiment_uuid="control",
        candidate_experiment_uuid="candidate",
        pairs=5,
        child_runner=_runner(
            [500.0, 510.0, 520.0, 530.0, 540.0],
            [480.0, 490.0, 500.0, 510.0, 520.0],
        ),
    )

    assert result["accepted"] is False
    assert result["reason_code"] == "warmup_no_material_benefit"


def test_warmup_benchmark_requires_exactly_five_pairs() -> None:
    with pytest.raises(ValueError, match="pairs must equal 5"):
        evaluate_pairs(
            programme_uuid="programme",
            control_experiment_uuid="control",
            candidate_experiment_uuid="candidate",
            pairs=4,
        )


def test_child_result_serializes_without_raw_content() -> None:
    result = ChildResult(
        arm="candidate",
        repetition_uuid="repetition",
        authentication_success=True,
        connect_ms=123.4,
        warmup_status="warmed",
        warmup_ms=100.0,
        token_request_count=1,
        model_session_count=1,
        response_request_count=0,
        audio_request_count=0,
    )

    serialized = json.dumps(asdict(result), sort_keys=True)

    assert "repetition" in serialized
    assert "transcript" not in serialized
    assert "audio" in serialized


def test_warmup_benchmark_writes_private_exclusive_evidence(tmp_path: Path) -> None:
    path = tmp_path / "private" / "result.json"
    _exclusive_write(path, {"accepted": False})

    assert json.loads(path.read_text(encoding="utf-8")) == {"accepted": False}
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        _exclusive_write(path, {"accepted": True})
