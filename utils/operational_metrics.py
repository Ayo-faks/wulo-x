"""Low-cardinality operational metrics for cost and startup readiness."""

from __future__ import annotations

import logging
import math
import os

from apps.artagent.backend.voice.shared.metrics_factory import (
    LazyCounter,
    LazyHistogram,
    LazyMeter,
)

logger = logging.getLogger(__name__)
_meter = LazyMeter("clinic_recall.operational", version="1.0.0")
_input_token_counter: LazyCounter = _meter.counter(
    "gen_ai.usage.input_tokens",
    "LLM input tokens consumed by completed operations",
    "token",
)
_output_token_counter: LazyCounter = _meter.counter(
    "gen_ai.usage.output_tokens",
    "LLM output tokens consumed by completed operations",
    "token",
)
_estimated_cost_histogram: LazyHistogram = _meter.histogram(
    "gen_ai.usage.estimated_cost_usd",
    "Estimated LLM cost using operator-configured per-million-token rates",
    "USD",
)
_warmup_duration_histogram: LazyHistogram = _meter.histogram(
    "startup.warmup.duration_ms",
    "Deferred warm-up duration",
    "ms",
)
_warmup_token_request_histogram: LazyHistogram = _meter.histogram(
    "startup.warmup.token_request_count",
    "VoiceLive token requests made during warm-up",
    "request",
)
_warmup_completion_counter: LazyCounter = _meter.counter(
    "startup.warmup.completed",
    "Completed deferred warm-up attempts",
    "1",
)


def _configured_rate(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid non-numeric operational metric rate: %s", name)
        return None
    if not math.isfinite(value) or value < 0:
        logger.warning("Ignoring invalid negative/non-finite operational metric rate: %s", name)
        return None
    return value


def estimate_genai_cost_usd(input_tokens: int, output_tokens: int) -> float | None:
    """Estimate cost only when both per-million-token rates are configured."""
    input_rate = _configured_rate("GENAI_INPUT_COST_PER_MILLION_TOKENS_USD")
    output_rate = _configured_rate("GENAI_OUTPUT_COST_PER_MILLION_TOKENS_USD")
    if input_rate is None or output_rate is None:
        return None
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


def record_genai_usage(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    model: str | None,
) -> float | None:
    """Record non-negative token totals and an optional configured cost estimate."""
    safe_input = max(0, int(input_tokens or 0))
    safe_output = max(0, int(output_tokens or 0))
    attributes = {"gen_ai.request.model": (model or "unknown").strip()[:128] or "unknown"}
    try:
        if safe_input:
            _input_token_counter.add(safe_input, attributes=attributes)
        if safe_output:
            _output_token_counter.add(safe_output, attributes=attributes)
        cost = estimate_genai_cost_usd(safe_input, safe_output)
        if cost is not None:
            _estimated_cost_histogram.record(cost, attributes=attributes)
        return cost
    except Exception:
        logger.exception("GenAI usage metric recording failed")
        return None


def record_warmup_metrics(
    *,
    duration_ms: float,
    success: bool,
    status: str,
    token_request_count: int,
) -> None:
    """Record one bounded deferred warm-up result without startup side effects."""
    attributes = {
        "warmup.success": success,
        "warmup.status": (status or "unknown").strip()[:64],
    }
    try:
        _warmup_duration_histogram.record(max(0.0, duration_ms), attributes=attributes)
        _warmup_token_request_histogram.record(
            max(0, token_request_count), attributes=attributes
        )
        _warmup_completion_counter.add(1, attributes=attributes)
    except Exception:
        logger.exception("Warm-up metric recording failed")