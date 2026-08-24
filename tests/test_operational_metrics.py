from __future__ import annotations

from types import SimpleNamespace

from apps.artagent.backend.voice.voicelive.orchestrator import LiveOrchestrator
from utils import operational_metrics


class _Counter:
    def __init__(self) -> None:
        self.values: list[tuple[int, dict]] = []

    def add(self, value: int, *, attributes: dict) -> None:
        self.values.append((value, attributes))


class _Histogram:
    def __init__(self) -> None:
        self.values: list[tuple[float, dict]] = []

    def record(self, value: float, *, attributes: dict) -> None:
        self.values.append((value, attributes))


class _VoiceMetrics:
    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.response_count = 0
        self.current_ttft_ms = None
        self.turn_count = 1

    def add_tokens(self, *, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def record_response(self) -> None:
        self.response_count += 1


def test_genai_usage_records_tokens_and_configured_cost(monkeypatch) -> None:
    input_counter = _Counter()
    output_counter = _Counter()
    cost_histogram = _Histogram()
    monkeypatch.setattr(operational_metrics, "_input_token_counter", input_counter)
    monkeypatch.setattr(operational_metrics, "_output_token_counter", output_counter)
    monkeypatch.setattr(operational_metrics, "_estimated_cost_histogram", cost_histogram)
    monkeypatch.setenv("GENAI_INPUT_COST_PER_MILLION_TOKENS_USD", "2.5")
    monkeypatch.setenv("GENAI_OUTPUT_COST_PER_MILLION_TOKENS_USD", "10")

    cost = operational_metrics.record_genai_usage(
        input_tokens=1000,
        output_tokens=200,
        model="model-v1",
    )

    assert cost == 0.0045
    assert input_counter.values == [(1000, {"gen_ai.request.model": "model-v1"})]
    assert output_counter.values == [(200, {"gen_ai.request.model": "model-v1"})]
    assert cost_histogram.values == [(0.0045, {"gen_ai.request.model": "model-v1"})]


def test_genai_cost_is_omitted_without_complete_pricing(monkeypatch) -> None:
    monkeypatch.delenv("GENAI_INPUT_COST_PER_MILLION_TOKENS_USD", raising=False)
    monkeypatch.delenv("GENAI_OUTPUT_COST_PER_MILLION_TOKENS_USD", raising=False)

    assert operational_metrics.estimate_genai_cost_usd(100, 20) is None


def test_warmup_metrics_are_bounded_and_low_cardinality(monkeypatch) -> None:
    duration = _Histogram()
    requests = _Histogram()
    completed = _Counter()
    monkeypatch.setattr(operational_metrics, "_warmup_duration_histogram", duration)
    monkeypatch.setattr(operational_metrics, "_warmup_token_request_histogram", requests)
    monkeypatch.setattr(operational_metrics, "_warmup_completion_counter", completed)

    operational_metrics.record_warmup_metrics(
        duration_ms=125.5,
        success=True,
        status="warmed",
        token_request_count=1,
    )

    attributes = {"warmup.success": True, "warmup.status": "warmed"}
    assert duration.values == [(125.5, attributes)]
    assert requests.values == [(1, attributes)]
    assert completed.values == [(1, attributes)]


def test_voicelive_response_records_operational_token_totals(monkeypatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(
        operational_metrics,
        "record_genai_usage",
        lambda **kwargs: captured.append(kwargs),
    )
    orchestrator = LiveOrchestrator.__new__(LiveOrchestrator)
    orchestrator._metrics = _VoiceMetrics()
    orchestrator._model_name = "gpt-realtime-1.5"
    orchestrator.messenger = None
    orchestrator.call_connection_id = None
    orchestrator._transport = "twilio"
    orchestrator.active = "InboundClinicAgent"
    event = SimpleNamespace(
        response=SimpleNamespace(
            id="response-1",
            status="completed",
            usage=SimpleNamespace(input_tokens=120, output_tokens=30),
        )
    )

    orchestrator._emit_model_metrics(event)

    assert captured == [
        {
            "input_tokens": 120,
            "output_tokens": 30,
            "model": "gpt-realtime-1.5",
        }
    ]
    assert orchestrator._metrics.input_tokens == 120
    assert orchestrator._metrics.output_tokens == 30
    assert orchestrator._metrics.response_count == 1