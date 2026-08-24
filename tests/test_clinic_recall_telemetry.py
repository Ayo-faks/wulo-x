from __future__ import annotations

import logging

import pytest
from apps.artagent.backend.voice.voicelive import orchestrator as orchestrator_module
from apps.artagent.backend.voice.voicelive.orchestrator import LiveOrchestrator
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from src.clinic_recall import telemetry


class _RecordingSpan:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def is_recording(self) -> bool:
        return True

    def add_event(self, name: str, *, attributes: dict) -> None:
        self.events.append((name, attributes))


def test_telemetry_logger_is_info_enabled() -> None:
    assert telemetry.logger.isEnabledFor(logging.INFO)


def test_queued_event_emits_only_after_commit(monkeypatch) -> None:
    span = _RecordingSpan()
    log_records: list[tuple[str, dict]] = []
    monkeypatch.setattr(telemetry, "_current_span", lambda: span)
    monkeypatch.setattr(
        telemetry.logger,
        "info",
        lambda message, *, extra: log_records.append((message, extra)),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with Session(engine) as session:
        session.execute(text("select 1"))
        telemetry.queue_after_commit(
            session,
            "voice.booking.created",
            {
                "channel": "call",
                "action_type": "book",
                "status": "completed",
                "queued_for_staff": False,
            },
        )
        assert span.events == []
        session.commit()

    assert span.events == [
        (
            "voice.booking.created",
            {
                "channel": "call",
                "action_type": "book",
                "status": "completed",
                "queued_for_staff": False,
            },
        )
    ]
    assert log_records == [
        (
            "Clinic Recall aggregate event",
            {
                "microsoft.custom_event.name": "voice.booking.created",
                "channel": "call",
                "action_type": "book",
                "status": "completed",
                "queued_for_staff": False,
            },
        )
    ]


def test_queued_event_is_discarded_on_rollback(monkeypatch) -> None:
    span = _RecordingSpan()
    monkeypatch.setattr(telemetry, "_current_span", lambda: span)
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with Session(engine) as session:
        session.execute(text("select 1"))
        telemetry.queue_after_commit(
            session,
            "voice.optout.recorded",
            {"channel": "sms"},
        )
        session.rollback()

    assert span.events == []


def test_event_contract_rejects_identifiers() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with Session(engine) as session:
        try:
            telemetry.queue_after_commit(
                session,
                "voice.optout.recorded",
                {"channel": "sms", "patient_id": "patient-1"},
            )
        except telemetry.ClinicRecallTelemetryError as exc:
            assert "patient_id" in str(exc)
        else:
            raise AssertionError("patient identifiers must be rejected")


def test_worker_summary_emits_only_closed_outcomes_including_zero(
    monkeypatch,
) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        telemetry,
        "emit_runtime_event",
        lambda name, attributes: events.append((name, dict(attributes))) or True,
    )

    assert telemetry.emit_worker_summary(
        "sms_dispatch",
        {"reconcile_required": 2, "dead_lettered": 0, "claimed": 9},
    )
    assert events == [
        (
            "worker.cycle.summary",
            {"worker": "sms_dispatch", "outcome": "reconcile_required", "count": 2},
        ),
        (
            "worker.cycle.summary",
            {"worker": "sms_dispatch", "outcome": "dead_lettered", "count": 0},
        ),
    ]


def test_worker_summary_rejects_unknown_worker_or_invalid_counter(monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        telemetry,
        "emit_runtime_event",
        lambda name, attributes: events.append((name, dict(attributes))) or True,
    )

    assert telemetry.emit_worker_summary("unknown", {}) is False
    assert telemetry.emit_worker_summary("call_dispatch", {"reconcile_required": True}) is False
    assert events == []


def test_worker_summary_swallows_unexpected_emitter_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        telemetry,
        "emit_runtime_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    assert telemetry.emit_worker_summary("call_dispatch", {"reconcile_required": 1}) is False


def test_job_telemetry_setup_is_optional_and_fail_soft(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.delenv("DISABLE_CLOUD_TELEMETRY", raising=False)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    assert telemetry.configure_job_telemetry(lambda **kwargs: calls.append(kwargs) or True) is False
    assert calls == []

    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    )
    assert telemetry.configure_job_telemetry(lambda **kwargs: calls.append(kwargs) or True) is True
    assert calls == [{"logger_name": ""}]
    assert (
        telemetry.configure_job_telemetry(
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
        )
        is False
    )


def test_pr14_event_contract_rejects_unbounded_values_and_counts() -> None:
    with pytest.raises(telemetry.ClinicRecallTelemetryError, match="worker/outcome"):
        telemetry._normalize_event(
            "worker.cycle.summary",
            {"worker": "sms_dispatch", "outcome": "conflicts", "count": 1},
        )
    with pytest.raises(telemetry.ClinicRecallTelemetryError, match="unsupported value"):
        telemetry._normalize_event(
            "callbacks.queue.snapshot",
            {
                "state": "tenant-specific-state",
                "oldest_age_bucket": "under_5m",
                "count": 1,
            },
        )
    with pytest.raises(telemetry.ClinicRecallTelemetryError, match="non-negative"):
        telemetry._normalize_event(
            "rights.deletion.overdue",
            {"kind": "target", "count": -1},
        )


def test_transaction_event_queue_is_bounded() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with Session(engine) as session:
        for _ in range(telemetry._MAX_EVENTS_PER_TRANSACTION):
            telemetry.queue_after_commit(
                session,
                "voice.optout.recorded",
                {"channel": "sms"},
            )
        try:
            telemetry.queue_after_commit(
                session,
                "voice.optout.recorded",
                {"channel": "sms"},
            )
        except telemetry.ClinicRecallTelemetryError as exc:
            assert str(exc) == "event queue limit exceeded"
        else:
            raise AssertionError("transaction event queue must be bounded")


class _EndMessenger:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def request_call_end(self, *, reason: str) -> None:
        self.reasons.append(reason)


async def test_call_outcome_emits_once_from_transport_end(monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        orchestrator_module,
        "emit_runtime_event",
        lambda name, attributes: events.append((name, dict(attributes))) or True,
    )
    messenger = _EndMessenger()
    orchestrator = LiveOrchestrator.__new__(LiveOrchestrator)
    orchestrator.messenger = messenger
    orchestrator._call_phase = "booking"
    orchestrator._call_outcome_emitted = False
    orchestrator._transport = "twilio"
    orchestrator._system_vars = {"scenario": "rebooking"}

    await orchestrator._request_call_end("booking_complete")
    await orchestrator._request_call_end("assistant_goodbye")

    assert messenger.reasons == ["booking_complete", "assistant_goodbye"]
    assert events == [
        (
            "voice.call.outcome",
            {"status": "booked", "transport": "twilio"},
        )
    ]
