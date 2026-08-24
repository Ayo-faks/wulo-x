"""Focused tests for the PR-14 read-only operational snapshot collector."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from src.clinic_recall.durable.callbacks import generate_effect_token
from src.clinic_recall.durable.config import operational_snapshot_enabled
from src.clinic_recall.enums import (
    ExternalEffectState,
    ExternalEffectType,
    ProviderCallbackKind,
    ProviderCallbackState,
)
from src.clinic_recall.models import (
    Base,
    Clinic,
    ExternalEffect,
    ProviderCallbackReceipt,
)
from src.clinic_recall.operational_snapshot import run_operational_snapshot_once
from src.clinic_recall.pilot_controls import OperationalSwitchSnapshot
from src.clinic_recall.rls import apply_rls_policies

CLINIC_ID = "clinic-pr14"
OTHER_CLINIC_ID = "clinic-other"
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, class_=Session)
    with factory() as session:
        session.add(Clinic(id=CLINIC_ID, name="PR14 Clinic", timezone="Europe/London"))
        session.add(Clinic(id=OTHER_CLINIC_ID, name="Other Clinic", timezone="Europe/London"))
        session.commit()
    return factory


def _fresh_switches(**overrides) -> OperationalSwitchSnapshot:
    values = {
        "outreach_enabled": False,
        "voice_enabled": False,
        "recording_enabled": False,
        "refreshed_at": NOW - timedelta(minutes=1),
        "max_age": timedelta(minutes=10),
        "environment": "staging",
        "release_identity": "release-a",
    }
    values.update(overrides)
    return OperationalSwitchSnapshot(**values)


def _effect(
    *,
    effect_id: str,
    state: ExternalEffectState,
    clinic_id: str = CLINIC_ID,
    effect_type: ExternalEffectType = ExternalEffectType.SMS,
    last_error_code: str | None = None,
    completed_at: datetime | None = None,
) -> ExternalEffect:
    return ExternalEffect(
        id=effect_id,
        clinic_id=clinic_id,
        aggregate_type="outreach_job",
        aggregate_id=f"job-{effect_id}",
        effect_type=effect_type,
        idempotency_key=f"idem-{effect_id}",
        callback_token=generate_effect_token(clinic_id),
        payload_version=1,
        payload={"intent": "pr14-test"},
        state=state,
        request_hash="0" * 64,
        last_error_code=last_error_code,
        completed_at=completed_at,
        available_at=NOW - timedelta(hours=1),
    )


def _collect(session_factory, *, switches=None, emit_log=None):
    events: list[tuple[str, dict]] = [] if emit_log is None else emit_log

    def emit(name, attributes):
        events.append((name, dict(attributes)))
        return True

    result = run_operational_snapshot_once(
        session_factory,
        clinic_id=CLINIC_ID,
        now=NOW,
        enabled=True,
        switches=switches or _fresh_switches(),
        emit=emit,
    )
    return result, events


def test_disabled_collector_reads_and_emits_nothing(session_factory) -> None:
    result = run_operational_snapshot_once(
        session_factory,
        clinic_id=CLINIC_ID,
        now=NOW,
        enabled=False,
    )
    assert result.enabled is False
    assert result.events_emitted == 0


def test_operational_snapshot_flag_is_false_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CLINIC_RECALL_OPERATIONAL_SNAPSHOT_ENABLED", raising=False)
    assert operational_snapshot_enabled() is False


def test_snapshot_cli_bootstraps_before_returning_disabled(monkeypatch, capsys) -> None:
    from src.clinic_recall import operational_snapshot as snapshot_module

    calls: list[str] = []
    monkeypatch.setattr(
        snapshot_module,
        "_bootstrap_runtime_configuration",
        lambda: calls.append("bootstrapped"),
    )
    monkeypatch.setattr(snapshot_module, "operational_snapshot_enabled", lambda: False)

    assert snapshot_module.main(["--clinic-id", CLINIC_ID]) == 0
    assert calls == ["bootstrapped"]
    assert '"enabled": false' in capsys.readouterr().out


def test_callback_snapshot_reports_lag_bucket_from_received_at(
    session_factory,
) -> None:
    with session_factory() as session:
        session.add(_effect(effect_id="e-cb", state=ExternalEffectState.DISPATCHING))
        session.flush()
        session.add(
            ProviderCallbackReceipt(
                id="cb-1",
                clinic_id=CLINIC_ID,
                external_effect_id="e-cb",
                provider="twilio",
                callback_kind=ProviderCallbackKind.SMS,
                deduplication_hash="1" * 64,
                effect_token_hash="2" * 64,
                normalized_status="queued",
                payload_hash="3" * 64,
                state=ProviderCallbackState.PENDING,
                received_at=NOW - timedelta(hours=2),
            )
        )
        session.add(
            ProviderCallbackReceipt(
                id="cb-2",
                clinic_id=CLINIC_ID,
                external_effect_id="e-cb",
                provider="twilio",
                callback_kind=ProviderCallbackKind.SMS,
                deduplication_hash="4" * 64,
                effect_token_hash="5" * 64,
                normalized_status="sent",
                payload_hash="6" * 64,
                state=ProviderCallbackState.PROCESSING,
                processing_attempts=1,
                lease_owner="worker-pr14",
                lease_expires_at=NOW - timedelta(minutes=1),
                received_at=NOW - timedelta(hours=3),
            )
        )
        session.commit()

    result, events = _collect(session_factory)
    snapshots = [attrs for name, attrs in events if name == "callbacks.queue.snapshot"]
    assert result.callback_groups == 2
    assert {
        (snapshot["state"], snapshot["oldest_age_bucket"], snapshot["count"])
        for snapshot in snapshots
    } == {
        ("pending", "1h_to_4h", 1),
        ("processing", "1h_to_4h", 1),
    }


def test_grounding_failure_counts_only_authority_invalid_cancellations(
    session_factory,
) -> None:
    with session_factory() as session:
        session.add(
            _effect(
                effect_id="e-grounding",
                state=ExternalEffectState.CANCELED,
                last_error_code="booking_confirmation_authority_invalid",
                completed_at=NOW - timedelta(hours=2),
            )
        )
        session.add(
            _effect(
                effect_id="e-policy-denial",
                state=ExternalEffectState.CANCELED,
                last_error_code="booking_confirmation_disabled",
                completed_at=NOW - timedelta(hours=2),
            )
        )
        session.add(
            _effect(
                effect_id="e-stale",
                state=ExternalEffectState.CANCELED,
                last_error_code="booking_confirmation_authority_invalid",
                completed_at=NOW - timedelta(days=3),
            )
        )
        session.commit()

    result, events = _collect(session_factory)
    blocked = [attrs for name, attrs in events if name == "booking.confirmation.blocked"]
    assert result.confirmation_grounding_failures == 1
    assert blocked == [{"reason_code": "booking_confirmation_authority_invalid", "count": 1}]


def test_healthy_snapshot_emits_zero_counts_and_fresh_status(session_factory) -> None:
    result, events = _collect(session_factory)
    by_name = {name: attrs for name, attrs in events}
    assert result.confirmation_grounding_failures == 0
    assert by_name["booking.confirmation.blocked"] == {
        "reason_code": "booking_confirmation_authority_invalid",
        "count": 0,
    }
    assert by_name["pilot.release.mismatch"] == {"count": 0}
    assert by_name["pilot.configuration.status"] == {"reason": "fresh", "count": 1}
    assert result.configuration_reason == "fresh"


def test_stale_configuration_reports_reason_while_fresh_does_not(
    session_factory,
) -> None:
    stale = _fresh_switches(refreshed_at=NOW - timedelta(hours=3))
    result, events = _collect(session_factory, switches=stale)
    status = [attrs for name, attrs in events if name == "pilot.configuration.status"]
    assert status == [{"reason": "configuration_stale", "count": 1}]
    assert result.configuration_reason == "configuration_stale"

    missing = _fresh_switches(refreshed_at=None)
    result, events = _collect(session_factory, switches=missing)
    status = [attrs for name, attrs in events if name == "pilot.configuration.status"]
    assert status == [{"reason": "configuration_evidence_missing", "count": 1}]

    oversized_ttl = _fresh_switches(max_age=timedelta(hours=1, seconds=1))
    result, events = _collect(session_factory, switches=oversized_ttl)
    status = [attrs for name, attrs in events if name == "pilot.configuration.status"]
    assert status == [{"reason": "configuration_evidence_missing", "count": 1}]


def test_emit_failure_never_raises_and_is_counted(session_factory) -> None:
    calls: list[str] = []

    def failing_emit(name, attributes):
        calls.append(name)
        return False

    result = run_operational_snapshot_once(
        session_factory,
        clinic_id=CLINIC_ID,
        now=NOW,
        enabled=True,
        switches=_fresh_switches(),
        emit=failing_emit,
    )
    assert result.emit_failures == len(calls) > 0
    assert result.events_emitted == 0


def test_emitter_exception_never_escapes_read_only_snapshot(session_factory) -> None:
    def raising_emit(_name, _attributes):
        raise RuntimeError("telemetry unavailable")

    result = run_operational_snapshot_once(
        session_factory,
        clinic_id=CLINIC_ID,
        now=NOW,
        enabled=True,
        switches=_fresh_switches(),
        emit=raising_emit,
    )
    assert result.emit_failures > 0
    assert result.events_emitted == 0


def test_lookback_and_timezone_bounds_fail_closed(session_factory) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run_operational_snapshot_once(
            session_factory,
            clinic_id=CLINIC_ID,
            now=datetime(2026, 7, 28, 12, 0),
            enabled=True,
        )
    with pytest.raises(ValueError, match="lookback"):
        run_operational_snapshot_once(
            session_factory,
            clinic_id=CLINIC_ID,
            now=NOW,
            enabled=True,
            lookback=timedelta(days=30),
        )


@pytest.mark.postgres
def test_postgres_snapshot_is_tenant_scoped_under_forced_rls(
    clinic_recall_pg_engine,
) -> None:
    engine = clinic_recall_pg_engine
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, class_=Session)
    with factory() as session:
        session.add(Clinic(id=CLINIC_ID, name="PR14 PostgreSQL A"))
        session.add(Clinic(id=OTHER_CLINIC_ID, name="PR14 PostgreSQL B"))
        for clinic_id, suffix in ((CLINIC_ID, "a"), (OTHER_CLINIC_ID, "b")):
            effect = _effect(
                effect_id=f"effect-pg-{suffix}",
                state=ExternalEffectState.DISPATCHING,
                clinic_id=clinic_id,
            )
            session.add(effect)
            session.flush()
            session.add(
                ProviderCallbackReceipt(
                    id=f"callback-pg-{suffix}",
                    clinic_id=clinic_id,
                    external_effect_id=effect.id,
                    provider="twilio",
                    callback_kind=ProviderCallbackKind.SMS,
                    deduplication_hash=suffix * 64,
                    effect_token_hash=("c" if suffix == "a" else "d") * 64,
                    normalized_status="queued",
                    payload_hash=("e" if suffix == "a" else "f") * 64,
                    state=ProviderCallbackState.PENDING,
                    received_at=NOW - timedelta(hours=2),
                )
            )
        session.commit()
    with engine.begin() as connection:
        apply_rls_policies(connection)

    result, events = _collect(factory)
    snapshots = [attrs for name, attrs in events if name == "callbacks.queue.snapshot"]
    assert result.callback_groups == 1
    assert snapshots == [{"state": "pending", "oldest_age_bucket": "1h_to_4h", "count": 1}]
