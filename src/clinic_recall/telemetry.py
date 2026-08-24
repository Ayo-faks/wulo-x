"""Aggregate-only telemetry emitted after deterministic writes commit."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session
from utils.ml_logging import get_logger

_EVENT_QUEUE_KEY = "clinic_recall.telemetry_events"
_MAX_EVENTS_PER_TRANSACTION = 100
_WORKER_SUMMARY_OUTCOMES = {
    "sms_dispatch": ("reconcile_required", "dead_lettered"),
    "call_dispatch": ("reconcile_required",),
    "recording_dispatch": ("reconcile_required",),
    "rights_dispatch": ("reconcile_required",),
    "rights_reconcile": ("reconcile_required",),
    "cliniko_dispatch": ("reconcile_required", "conflicts", "dead_lettered"),
    "cliniko_reconcile": ("unresolved", "conflicts", "exhausted"),
    "callback_reconcile": ("conflicts",),
}
_AGE_BUCKETS = frozenset({"under_5m", "5m_to_15m", "15m_to_1h", "1h_to_4h", "over_4h"})
_EVENT_STRING_VALUES = {
    "worker.cycle.summary": {
        "worker": frozenset(_WORKER_SUMMARY_OUTCOMES),
        "outcome": frozenset(
            outcome for outcomes in _WORKER_SUMMARY_OUTCOMES.values() for outcome in outcomes
        ),
    },
    "callbacks.queue.snapshot": {
        "state": frozenset({"pending", "processing"}),
        "oldest_age_bucket": _AGE_BUCKETS,
    },
    "booking.confirmation.blocked": {
        "reason_code": frozenset({"booking_confirmation_authority_invalid"}),
    },
    "recording.consent.mismatch": {
        "reason_code": frozenset(
            {"provider_outcome_conflict", "recording_status_reconcile_required"}
        ),
    },
    "rights.deletion.overdue": {
        "kind": frozenset({"request", "target", "residual"}),
    },
    "pilot.invariant.violation": {
        "reason_code": frozenset(
            {
                "cohort_limit_exceeded",
                "cumulative_limit_invalid",
                "participant_wave_invalid",
                "programme_release_conflict",
                "programme_state_invalid",
            }
        ),
    },
    "pilot.configuration.status": {
        "reason": frozenset(
            {
                "fresh",
                "configuration_identity_missing",
                "configuration_evidence_missing",
                "configuration_stale",
            }
        ),
    },
}
_NONNEGATIVE_COUNT_EVENTS = frozenset(
    {
        "worker.cycle.summary",
        "callbacks.queue.snapshot",
        "booking.confirmation.blocked",
        "recording.consent.mismatch",
        "rights.deletion.overdue",
        "pilot.invariant.violation",
        "pilot.configuration.status",
        "pilot.release.mismatch",
    }
)
_EVENT_ATTRIBUTES = {
    "voice.booking.created": frozenset({"channel", "action_type", "status", "queued_for_staff"}),
    "voice.escalation.triggered": frozenset({"channel", "reason", "priority"}),
    "voice.optout.recorded": frozenset({"channel"}),
    "voice.call.outcome": frozenset({"status", "transport"}),
    "voice.call.status": frozenset({"provider", "status", "answered", "terminal"}),
    "sms.delivery.updated": frozenset({"provider", "channel", "status", "successful"}),
    "outreach.message.sent": frozenset(
        {"provider", "channel", "status", "successful", "message_kind"}
    ),
    "handoff.unknown_reason": frozenset({"owner_kind"}),
    "handoff.alternate.requested": frozenset({"severity", "reason_code"}),
    "handoff.programme.pause": frozenset({"reason_code", "outcome"}),
    "handoff.queue.snapshot": frozenset(
        {"severity", "delivery_state", "oldest_age_bucket", "count"}
    ),
    "handoff.sla.breach": frozenset({"severity", "count"}),
    "handoff.notification.outcome": frozenset({"outcome", "count"}),
    "worker.cycle.summary": frozenset({"worker", "outcome", "count"}),
    "callbacks.queue.snapshot": frozenset({"state", "oldest_age_bucket", "count"}),
    "booking.confirmation.blocked": frozenset({"reason_code", "count"}),
    "recording.consent.mismatch": frozenset({"reason_code", "count"}),
    "rights.deletion.overdue": frozenset({"kind", "count"}),
    "pilot.invariant.violation": frozenset({"reason_code", "count"}),
    "pilot.configuration.status": frozenset({"reason", "count"}),
    "pilot.release.mismatch": frozenset({"count"}),
}
logger = get_logger("clinic_recall.telemetry")


class ClinicRecallTelemetryError(ValueError):
    """Raised when an event attempts to leave the aggregate-only contract."""


def queue_after_commit(
    session: Session,
    name: str,
    attributes: Mapping[str, str | bool | int | float],
) -> None:
    """Queue one allow-listed event for emission after the outer transaction commits."""
    normalized = _normalize_event(name, attributes)
    queue = session.info.setdefault(_EVENT_QUEUE_KEY, [])
    if len(queue) >= _MAX_EVENTS_PER_TRANSACTION:
        raise ClinicRecallTelemetryError("event queue limit exceeded")
    queue.append((name, normalized))


def emit_runtime_event(
    name: str,
    attributes: Mapping[str, str | bool | int | float],
) -> bool:
    """Emit an allow-listed non-transactional event without affecting runtime flow."""
    try:
        normalized = _normalize_event(name, attributes)
        _publish_event(name, normalized)
        return True
    except Exception:
        logger.exception("Clinic Recall aggregate runtime telemetry emission failed")
        return False


def configure_job_telemetry(setup: Any | None = None) -> bool:
    """Attach Azure Monitor logging for a short-lived Job when configured."""
    if (
        os.getenv("DISABLE_CLOUD_TELEMETRY", "false").strip().lower() == "true"
        or not os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    ):
        return False
    try:
        if setup is None:
            from utils.telemetry_config import setup_azure_monitor

            setup = setup_azure_monitor
        return bool(setup(logger_name=""))
    except Exception:
        logger.exception("Clinic Recall Job telemetry setup failed")
        return False


def emit_worker_summary(
    worker: str,
    summary: Mapping[str, int | bool],
) -> bool:
    """Emit only closed counters from one completed bounded worker cycle."""
    try:
        outcomes = _WORKER_SUMMARY_OUTCOMES.get(worker)
        if outcomes is None:
            logger.error("Unsupported Clinic Recall worker telemetry contract")
            return False
        emitted = True
        for outcome in outcomes:
            value = summary.get(outcome)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                logger.error("Invalid Clinic Recall aggregate worker counter")
                emitted = False
                continue
            emitted = (
                emit_runtime_event(
                    "worker.cycle.summary",
                    {"worker": worker, "outcome": outcome, "count": value},
                )
                and emitted
            )
        return emitted
    except Exception:
        logger.exception("Clinic Recall aggregate worker telemetry emission failed")
        return False


def _normalize_event(
    name: str,
    attributes: Mapping[str, str | bool | int | float],
) -> dict[str, str | bool | int | float]:
    allowed = _EVENT_ATTRIBUTES.get(name)
    if allowed is None:
        raise ClinicRecallTelemetryError(f"unsupported Clinic Recall event: {name}")
    unknown = set(attributes) - allowed
    if unknown:
        raise ClinicRecallTelemetryError(
            f"unsupported attributes for {name}: {', '.join(sorted(unknown))}"
        )
    if name == "worker.cycle.summary":
        worker = attributes.get("worker")
        outcome = attributes.get("outcome")
        if not isinstance(worker, str) or outcome not in _WORKER_SUMMARY_OUTCOMES.get(worker, ()):
            raise ClinicRecallTelemetryError("unsupported worker/outcome telemetry combination")
    normalized: dict[str, str | bool | int | float] = {}
    closed_values = _EVENT_STRING_VALUES.get(name, {})
    for key, value in attributes.items():
        if isinstance(value, str):
            normalized_value = value.strip()[:64]
            allowed_values = closed_values.get(key)
            if allowed_values is not None and normalized_value not in allowed_values:
                raise ClinicRecallTelemetryError(
                    f"unsupported value for {name}.{key}: {normalized_value!r}"
                )
            normalized[key] = normalized_value
        elif isinstance(value, bool | int | float):
            if (
                key == "count"
                and name in _NONNEGATIVE_COUNT_EVENTS
                and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
            ):
                raise ClinicRecallTelemetryError(f"{name}.count must be a non-negative integer")
            normalized[key] = value
        else:
            raise ClinicRecallTelemetryError(f"attribute {key!r} must be scalar")
    return normalized


def _current_span() -> Any:
    from opentelemetry import trace

    return trace.get_current_span()


def _publish_event(
    name: str,
    attributes: Mapping[str, str | bool | int | float],
) -> None:
    span = _current_span()
    if span.is_recording():
        span.add_event(name, attributes=attributes)
    logger.info(
        "Clinic Recall aggregate event",
        extra={"microsoft.custom_event.name": name, **attributes},
    )


@event.listens_for(Session, "after_commit")
def _emit_after_commit(session: Session) -> None:
    queued = session.info.pop(_EVENT_QUEUE_KEY, [])
    if not queued:
        return
    try:
        for name, attributes in queued:
            _publish_event(name, attributes)
    except Exception:
        logger.exception("Clinic Recall aggregate telemetry emission failed after commit")


@event.listens_for(Session, "after_rollback")
def _discard_after_rollback(session: Session) -> None:
    session.info.pop(_EVENT_QUEUE_KEY, None)
