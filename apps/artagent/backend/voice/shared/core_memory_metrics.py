"""
Core Memory Metrics Bridge
==========================

Lightweight bridge to store key performance metrics in session core memory
for frontend consumption, WITHOUT impacting the hot path.

This module:
- Stores only essential metrics needed by the SessionPerformancePanel
- Updates core memory asynchronously in the background
- Maintains the existing OpenTelemetry pipeline intact
- Keeps a rolling window of recent metrics

Usage:
    from voice.shared.core_memory_metrics import update_core_memory_metrics

    # Call asynchronously, off the hot path
    asyncio.create_task(update_core_memory_metrics(
        memo_manager=mm,
        session_id=session_id,
        metric_type="llm_ttft",
        value_ms=123.45,
        metadata={"agent": "Concierge", "model": "gpt-4o"}
    ))
"""

import asyncio
import time
from typing import Any, Dict, Optional
from utils.ml_logging import get_logger

try:
    from src.stateful.state_managment import MemoManager
except ImportError:
    MemoManager = None

logger = get_logger("voice.core_memory_metrics")


async def update_core_memory_metrics(
    memo_manager: Optional["MemoManager"],
    session_id: str,
    metric_type: str,
    value_ms: float,
    metadata: Optional[Dict[str, Any]] = None,
    turn_number: Optional[int] = None,
) -> None:
    """
    Update session core memory with performance metrics (async, off hot path).

    Args:
        memo_manager: Session memo manager
        session_id: Session identifier
        metric_type: Type of metric (llm_ttft, tts_ttfb, stt_latency, turn_duration)
        value_ms: Metric value in milliseconds
        metadata: Optional metadata (agent name, model, etc.)
        turn_number: Turn number for correlation
    """
    if not memo_manager or not session_id:
        return

    try:
        # Get current latency data structure
        latency_data = memo_manager.get_value_from_corememory("latency") or {
            "current_turn": {},
            "recent_turns": [],
            "summary": {
                "avg_llm_ttft": 0,
                "avg_tts_ttfb": 0,
                "avg_stt_latency": 0,
                "avg_turn_duration": 0,
                "total_turns": 0,
            }
        }

        # Update current turn metrics
        current_turn = latency_data.get("current_turn", {})
        current_turn[metric_type] = {
            "value_ms": value_ms,
            "timestamp": time.time(),
            "metadata": metadata or {},
            "turn_number": turn_number,
        }
        latency_data["current_turn"] = current_turn

        # If this is a turn completion, move to recent_turns
        if metric_type == "turn_duration":
            # Add completed turn to recent history
            recent_turns = latency_data.get("recent_turns", [])
            recent_turns.append({
                "turn_number": turn_number,
                "timestamp": time.time(),
                "metrics": dict(current_turn),  # Copy current turn data
            })

            # Keep only last 10 turns for performance
            if len(recent_turns) > 10:
                recent_turns = recent_turns[-10:]

            latency_data["recent_turns"] = recent_turns

            # Update summary statistics
            _update_summary_stats(latency_data, recent_turns)

            # Clear current turn for next one
            latency_data["current_turn"] = {}

        # Store back to core memory
        memo_manager.set_corememory("latency", latency_data)

        logger.debug(
            f"📊 Core memory updated: {metric_type}={value_ms:.1f}ms | session={session_id} turn={turn_number}"
        )

    except Exception as e:
        # Don't let core memory updates break anything - just log and continue
        logger.debug(f"Core memory metrics update failed (non-critical): {e}")


def _update_summary_stats(latency_data: Dict[str, Any], recent_turns: list) -> None:
    """Update summary statistics from recent turns."""
    if not recent_turns:
        return

    # Calculate averages from recent turns
    metrics_sums = {
        "llm_ttft": 0,
        "tts_ttfb": 0,
        "stt_latency": 0,
        "turn_duration": 0,
    }
    metrics_counts = {key: 0 for key in metrics_sums}

    for turn in recent_turns:
        for metric_type, data in turn.get("metrics", {}).items():
            if metric_type in metrics_sums:
                metrics_sums[metric_type] += data.get("value_ms", 0)
                metrics_counts[metric_type] += 1

    # Calculate averages
    summary = latency_data["summary"]
    summary["avg_llm_ttft"] = (metrics_sums["llm_ttft"] / metrics_counts["llm_ttft"]) if metrics_counts["llm_ttft"] > 0 else 0
    summary["avg_tts_ttfb"] = (metrics_sums["tts_ttfb"] / metrics_counts["tts_ttfb"]) if metrics_counts["tts_ttfb"] > 0 else 0
    summary["avg_stt_latency"] = (metrics_sums["stt_latency"] / metrics_counts["stt_latency"]) if metrics_counts["stt_latency"] > 0 else 0
    summary["avg_turn_duration"] = (metrics_sums["turn_duration"] / metrics_counts["turn_duration"]) if metrics_counts["turn_duration"] > 0 else 0
    summary["total_turns"] = len(recent_turns)


def schedule_core_memory_update(
    memo_manager: Optional["MemoManager"],
    session_id: str,
    metric_type: str,
    value_ms: float,
    metadata: Optional[Dict[str, Any]] = None,
    turn_number: Optional[int] = None,
) -> None:
    """
    Schedule a core memory update task (non-blocking, fire-and-forget).

    This function can be called from the hot path safely.
    """
    if not memo_manager:
        return

    try:
        # Schedule the update as a background task
        asyncio.create_task(update_core_memory_metrics(
            memo_manager=memo_manager,
            session_id=session_id,
            metric_type=metric_type,
            value_ms=value_ms,
            metadata=metadata,
            turn_number=turn_number,
        ))
    except Exception as e:
        # Silently handle any task creation failures
        logger.debug(f"Failed to schedule core memory update: {e}")


__all__ = [
    "update_core_memory_metrics",
    "schedule_core_memory_update",
]