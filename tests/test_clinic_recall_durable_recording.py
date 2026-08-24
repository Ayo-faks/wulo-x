"""Durable provider start/stop contracts for PR-09 recording."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.durable import recording_worker as recording_worker_module
from src.clinic_recall.durable.callbacks import receive_twilio_callback
from src.clinic_recall.durable.recording_worker import run_once
from src.clinic_recall.enums import (
    CallRecordingStatus,
    ClinicPhoneProvider,
    ExternalEffectState,
    ExternalEffectType,
    InboundCallStatus,
    InteractionDirection,
    PilotProgrammeState,
    ProviderCallbackKind,
    RecordingConsentState,
)
from src.clinic_recall.models import (
    Base,
    CallRecord,
    Clinic,
    ExternalEffect,
    InboundCall,
    PilotProgramme,
)
from src.clinic_recall.recording import (
    RecordingProviderDisposition,
    RecordingProviderReason,
    RecordingProviderResult,
    TwilioRecordingProvider,
    enforce_recording_switch_off,
    ensure_call_record,
    request_recording_start,
    request_recording_stop,
    withdraw_recording_consent,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
CALL_SID = "CA" + "a" * 32
RECORDING_SID = "RE" + "b" * 32


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    result = sessionmaker(bind=engine, expire_on_commit=False)
    with result.begin() as session:
        session.add(Clinic(id="clinic-a", name="Clinic A"))
        ledger = ensure_call_record(
            session,
            "clinic-a",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=CALL_SID,
            session_id="recording-worker-session",
            direction=InteractionDirection.INBOUND,
            scenario="inbound_clinic",
            patient_id=None,
            consent_snapshot=None,
            now=NOW,
        )
        ledger.consent_state = RecordingConsentState.GRANTED
        ledger.consent_version = "synthetic-pr09-v1"
    return result


class _StaticProvider:
    name = "synthetic-recording"

    def __init__(
        self,
        *,
        start: RecordingProviderResult | None = None,
        stop: RecordingProviderResult | None = None,
        factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.start_result = start
        self.stop_result = stop
        self.factory = factory
        self.start_calls = 0
        self.stop_calls = 0

    def start_recording(self, *, call_sid: str, callback_url: str) -> RecordingProviderResult:
        self.start_calls += 1
        assert call_sid == CALL_SID
        assert callback_url
        if self.factory is not None:
            with self.factory() as session:
                effect = (
                    session.query(ExternalEffect)
                    .filter_by(effect_type=ExternalEffectType.RECORDING)
                    .one()
                )
                assert effect.state == ExternalEffectState.DISPATCHING
                ledger = session.query(CallRecord).one()
                assert ledger.recording_status == CallRecordingStatus.STARTING
        assert self.start_result is not None
        return self.start_result

    def stop_recording(self, *, call_sid: str, recording_sid: str) -> RecordingProviderResult:
        self.stop_calls += 1
        assert call_sid == CALL_SID
        assert recording_sid == RECORDING_SID
        assert self.stop_result is not None
        return self.stop_result


def _ledger(factory: sessionmaker[Session]) -> CallRecord:
    with factory() as session:
        return session.query(CallRecord).one()


def test_recording_start_effect_is_minimized_idempotent_and_bound(factory) -> None:
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        first, created = request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )
        second, created_again = request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )

    assert created is True
    assert created_again is False
    assert second.id == first.id
    assert first.aggregate_type == "call_record"
    assert first.aggregate_id == ledger.id
    assert first.effect_type == ExternalEffectType.RECORDING
    assert first.max_attempts == 1
    assert first.payload == {"intent": "recording_start", "call_record_id": ledger.id}
    assert CALL_SID not in str(first.payload)
    assert _ledger(factory).recording_status == CallRecordingStatus.START_PENDING


def test_recording_start_requires_per_call_grant(factory) -> None:
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        ledger.consent_state = RecordingConsentState.DECLINED
        with pytest.raises(ValueError, match="granted"):
            request_recording_start(
                session,
                clinic_id="clinic-a",
                call_record_id=ledger.id,
                now=NOW,
            )


def test_start_success_uses_twilio_webhook_base_url(factory, monkeypatch) -> None:
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    monkeypatch.delenv("BASE_URL", raising=False)
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )
    provider = _StaticProvider(
        start=RecordingProviderResult(
            disposition=RecordingProviderDisposition.ACCEPTED,
            reason=RecordingProviderReason.PROVIDER_ACCEPTED,
            recording_sid=RECORDING_SID,
        ),
        factory=factory,
    )

    result = run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-start-worker",
        provider=provider,
        start_gate=lambda *_args: True,
        now=NOW,
        enabled=True,
    )

    ledger = _ledger(factory)
    assert result.started == 1
    assert provider.start_calls == 1
    assert ledger.recording_sid == RECORDING_SID
    assert ledger.recording_status == CallRecordingStatus.IN_PROGRESS
    assert ledger.recording_started_at == NOW


def test_ambiguous_start_is_quarantined_and_never_replayed(factory, monkeypatch) -> None:
    monkeypatch.setenv("BASE_URL", "https://clinic.example.test")
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        request_recording_start(session, clinic_id="clinic-a", call_record_id=ledger.id, now=NOW)
    provider = _StaticProvider(
        start=RecordingProviderResult(
            disposition=RecordingProviderDisposition.AMBIGUOUS,
            reason=RecordingProviderReason.TRANSPORT_ERROR,
        )
    )

    first = run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-ambiguous-worker",
        provider=provider,
        start_gate=lambda *_args: True,
        now=NOW,
        enabled=True,
    )
    second = run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-ambiguous-replay",
        provider=provider,
        start_gate=lambda *_args: True,
        now=NOW,
        enabled=True,
    )

    assert first.reconcile_required == 1
    assert second.claimed == 0
    assert provider.start_calls == 1
    assert _ledger(factory).recording_status == CallRecordingStatus.RECONCILE_REQUIRED


def test_provider_exception_quarantines_effect_and_ledger(factory, monkeypatch) -> None:
    monkeypatch.setenv("BASE_URL", "https://clinic.example.test")
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )

    class _RaisingProvider:
        name = "synthetic-raising-recording"

        def start_recording(self, **_kwargs):
            raise RuntimeError("private provider detail")

        def stop_recording(self, **_kwargs):
            raise AssertionError("stop must not be called")

    result = run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-exception-worker",
        provider=_RaisingProvider(),
        start_gate=lambda *_args: True,
        now=NOW,
        enabled=True,
    )

    with factory() as session:
        effect = session.query(ExternalEffect).one()
        ledger = session.query(CallRecord).one()
        assert result.reconcile_required == 1
        assert effect.state == ExternalEffectState.RECONCILE_REQUIRED
        assert effect.last_error_code == "provider_outcome_unknown"
        assert "private" not in repr(effect.__dict__).lower()
        assert ledger.recording_status == CallRecordingStatus.RECONCILE_REQUIRED


def test_withdrawal_during_accepted_start_immediately_queues_exact_stop(
    factory,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BASE_URL", "https://clinic.example.test")
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )

    class _WithdrawDuringStart:
        name = "synthetic-withdraw-during-start"

        def start_recording(self, **_kwargs):
            with factory.begin() as session:
                ledger = session.query(CallRecord).one()
                assert ledger.recording_status == CallRecordingStatus.STARTING
                withdraw_recording_consent(
                    session,
                    clinic_id="clinic-a",
                    call_record_id=ledger.id,
                    source="dtmf",
                    now=NOW,
                )
            return RecordingProviderResult(
                disposition=RecordingProviderDisposition.ACCEPTED,
                reason=RecordingProviderReason.PROVIDER_ACCEPTED,
                recording_sid=RECORDING_SID,
            )

        def stop_recording(self, **_kwargs):
            raise AssertionError("stop is a separately committed effect")

    result = run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-withdraw-race-worker",
        provider=_WithdrawDuringStart(),
        start_gate=lambda *_args: True,
        now=NOW,
        enabled=True,
    )

    with factory() as session:
        ledger = session.query(CallRecord).one()
        effects = session.query(ExternalEffect).all()
        assert result.started == 1
        assert ledger.consent_state == RecordingConsentState.WITHDRAWN
        assert ledger.recording_sid == RECORDING_SID
        assert ledger.recording_status == CallRecordingStatus.STOP_PENDING
        assert any(effect.payload["intent"] == "recording_stop" for effect in effects)


def test_in_progress_callback_recovers_lost_start_response(factory, monkeypatch) -> None:
    monkeypatch.setenv("BASE_URL", "https://clinic.example.test")
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        effect, _created = request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )
        effect_token = effect.callback_token
    provider = _StaticProvider(
        start=RecordingProviderResult(
            disposition=RecordingProviderDisposition.AMBIGUOUS,
            reason=RecordingProviderReason.TRANSPORT_ERROR,
        )
    )
    run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-lost-response-worker",
        provider=provider,
        start_gate=lambda *_args: True,
        now=NOW,
        enabled=True,
    )

    with factory.begin() as session:
        receive_twilio_callback(
            session,
            effect_token=effect_token,
            callback_kind=ProviderCallbackKind.RECORDING,
            fields={
                "RecordingSid": RECORDING_SID,
                "RecordingStatus": "in-progress",
                "RecordingStartTime": "Tue, 21 Jul 2026 12:00:00 +0000",
            },
            raw_payload=b"minimized-recording-in-progress",
            received_at=NOW,
        )

    with factory() as session:
        ledger = session.query(CallRecord).one()
        effect = session.query(ExternalEffect).one()
        assert effect.state == ExternalEffectState.SUCCEEDED
        assert effect.provider_resource_id == RECORDING_SID
        assert ledger.recording_sid == RECORDING_SID
        assert ledger.recording_status == CallRecordingStatus.IN_PROGRESS
        assert ledger.recording_started_at == NOW


def test_conflicting_callback_sid_quarantines_without_rebinding(factory, monkeypatch) -> None:
    monkeypatch.setenv("BASE_URL", "https://clinic.example.test")
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        effect, _created = request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )
        effect_token = effect.callback_token
    provider = _StaticProvider(
        start=RecordingProviderResult(
            disposition=RecordingProviderDisposition.ACCEPTED,
            reason=RecordingProviderReason.PROVIDER_ACCEPTED,
            recording_sid=RECORDING_SID,
        )
    )
    run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-conflict-worker",
        provider=provider,
        start_gate=lambda *_args: True,
        now=NOW,
        enabled=True,
    )

    with factory.begin() as session:
        receive_twilio_callback(
            session,
            effect_token=effect_token,
            callback_kind=ProviderCallbackKind.RECORDING,
            fields={
                "RecordingSid": "RE" + "c" * 32,
                "RecordingStatus": "in-progress",
            },
            raw_payload=b"conflicting-recording-identity",
            received_at=NOW,
        )

    with factory() as session:
        ledger = session.query(CallRecord).one()
        effect = session.query(ExternalEffect).one()
        assert effect.state == ExternalEffectState.RECONCILE_REQUIRED
        assert ledger.recording_sid == RECORDING_SID
        assert ledger.recording_status == CallRecordingStatus.RECONCILE_REQUIRED


def test_callback_after_ambiguous_start_honors_prior_withdrawal(factory, monkeypatch) -> None:
    monkeypatch.setenv("BASE_URL", "https://clinic.example.test")
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        effect, _created = request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )
        effect_token = effect.callback_token
    provider = _StaticProvider(
        start=RecordingProviderResult(
            disposition=RecordingProviderDisposition.AMBIGUOUS,
            reason=RecordingProviderReason.TRANSPORT_ERROR,
        )
    )
    run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-withdraw-race-worker",
        provider=provider,
        start_gate=lambda *_args: True,
        now=NOW,
        enabled=True,
    )
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        withdraw_recording_consent(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            source="dtmf",
            now=NOW,
        )
        receive_twilio_callback(
            session,
            effect_token=effect_token,
            callback_kind=ProviderCallbackKind.RECORDING,
            fields={
                "RecordingSid": RECORDING_SID,
                "RecordingStatus": "in-progress",
            },
            raw_payload=b"late-provider-start-after-withdrawal",
            received_at=NOW,
        )

    with factory() as session:
        ledger = session.query(CallRecord).one()
        effects = session.query(ExternalEffect).order_by(ExternalEffect.id).all()
        assert ledger.consent_state == RecordingConsentState.WITHDRAWN
        assert ledger.recording_sid == RECORDING_SID
        assert ledger.recording_status == CallRecordingStatus.STOP_PENDING
        assert len(effects) == 2
        assert any(effect.payload["intent"] == "recording_stop" for effect in effects)


def test_callback_after_ambiguous_start_honors_prior_switch_off(factory, monkeypatch) -> None:
    monkeypatch.setenv("BASE_URL", "https://clinic.example.test")
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        effect, _created = request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )
        effect_token = effect.callback_token
    run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-switch-race-worker",
        provider=_StaticProvider(
            start=RecordingProviderResult(
                disposition=RecordingProviderDisposition.AMBIGUOUS,
                reason=RecordingProviderReason.TRANSPORT_ERROR,
            )
        ),
        start_gate=lambda *_args: True,
        now=NOW,
        enabled=True,
    )
    with factory.begin() as session:
        first = enforce_recording_switch_off(
            session,
            clinic_id="clinic-a",
            now=NOW,
            reason_code="recording_switch_disabled",
        )
        assert first.reconcile_required == 1
        ledger = session.query(CallRecord).one()
        assert ledger.consent_state == RecordingConsentState.GRANTED
        assert ledger.recording_stop_requested_at == NOW
        receive_twilio_callback(
            session,
            effect_token=effect_token,
            callback_kind=ProviderCallbackKind.RECORDING,
            fields={
                "RecordingSid": RECORDING_SID,
                "RecordingStatus": "in-progress",
            },
            raw_payload=b"late-provider-start-after-switch-off",
            received_at=NOW,
        )

    with factory() as session:
        ledger = session.query(CallRecord).one()
        effects = session.query(ExternalEffect).all()
        assert ledger.consent_state == RecordingConsentState.GRANTED
        assert ledger.recording_sid == RECORDING_SID
        assert ledger.recording_status == CallRecordingStatus.STOP_PENDING
        assert any(effect.payload["intent"] == "recording_stop" for effect in effects)


def test_stop_dispatches_when_start_gate_is_false(factory) -> None:
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        ledger.recording_sid = RECORDING_SID
        ledger.recording_status = CallRecordingStatus.IN_PROGRESS
        ledger.recording_started_at = NOW
        ledger.consent_state = RecordingConsentState.WITHDRAWN
        request_recording_stop(session, clinic_id="clinic-a", call_record_id=ledger.id, now=NOW)
    provider = _StaticProvider(
        stop=RecordingProviderResult(
            disposition=RecordingProviderDisposition.ACCEPTED,
            reason=RecordingProviderReason.PROVIDER_ACCEPTED,
            recording_sid=RECORDING_SID,
        )
    )

    result = run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-stop-worker",
        provider=provider,
        start_gate=lambda *_args: False,
        now=NOW,
        enabled=True,
    )

    ledger = _ledger(factory)
    assert result.stopped == 1
    assert provider.stop_calls == 1
    assert ledger.recording_status == CallRecordingStatus.COMPLETED
    assert ledger.recording_stopped_at == NOW


def test_ambiguous_stop_never_claims_recording_stopped(factory) -> None:
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        ledger.recording_sid = RECORDING_SID
        ledger.recording_status = CallRecordingStatus.IN_PROGRESS
        ledger.recording_started_at = NOW
        ledger.consent_state = RecordingConsentState.WITHDRAWN
        request_recording_stop(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )
    provider = _StaticProvider(
        stop=RecordingProviderResult(
            disposition=RecordingProviderDisposition.AMBIGUOUS,
            reason=RecordingProviderReason.TRANSPORT_ERROR,
        )
    )

    result = run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-stop-ambiguous-worker",
        provider=provider,
        start_gate=lambda *_args: False,
        now=NOW,
        enabled=True,
    )

    ledger = _ledger(factory)
    assert result.reconcile_required == 1
    assert provider.stop_calls == 1
    assert ledger.recording_status == CallRecordingStatus.RECONCILE_REQUIRED
    assert ledger.recording_stopped_at is None


def test_completed_callback_settles_ambiguous_stop_effect(factory, monkeypatch) -> None:
    monkeypatch.setenv("BASE_URL", "https://clinic.example.test")
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        start_effect, _created = request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )
        start_token = start_effect.callback_token
    run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-start-before-stop-worker",
        provider=_StaticProvider(
            start=RecordingProviderResult(
                disposition=RecordingProviderDisposition.ACCEPTED,
                reason=RecordingProviderReason.PROVIDER_ACCEPTED,
                recording_sid=RECORDING_SID,
            )
        ),
        start_gate=lambda *_args: True,
        now=NOW,
        enabled=True,
    )
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        withdraw_recording_consent(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            source="dtmf",
            now=NOW,
        )
    run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-ambiguous-stop-worker",
        provider=_StaticProvider(
            stop=RecordingProviderResult(
                disposition=RecordingProviderDisposition.AMBIGUOUS,
                reason=RecordingProviderReason.TRANSPORT_ERROR,
            )
        ),
        start_gate=lambda *_args: False,
        now=NOW,
        enabled=True,
    )

    with factory.begin() as session:
        receive_twilio_callback(
            session,
            effect_token=start_token,
            callback_kind=ProviderCallbackKind.RECORDING,
            fields={
                "RecordingSid": RECORDING_SID,
                "RecordingStatus": "completed",
            },
            raw_payload=b"recording-completed-after-ambiguous-stop",
            received_at=NOW,
        )

    with factory() as session:
        ledger = session.query(CallRecord).one()
        stop_effect = next(
            effect
            for effect in session.query(ExternalEffect).all()
            if effect.payload["intent"] == "recording_stop"
        )
        assert ledger.recording_status == CallRecordingStatus.COMPLETED
        assert ledger.recording_stopped_at == NOW
        assert stop_effect.state == ExternalEffectState.SUCCEEDED
        assert stop_effect.provider_resource_id == RECORDING_SID
        assert stop_effect.provider_status == "recording_completed"
        assert stop_effect.completion_evidence_hash is not None


def test_delayed_in_progress_callback_cannot_resurrect_completed_recording(
    factory,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BASE_URL", "https://clinic.example.test")
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        start_effect, _created = request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )
        start_token = start_effect.callback_token
    run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-start-before-delayed-callback",
        provider=_StaticProvider(
            start=RecordingProviderResult(
                disposition=RecordingProviderDisposition.ACCEPTED,
                reason=RecordingProviderReason.PROVIDER_ACCEPTED,
                recording_sid=RECORDING_SID,
            )
        ),
        start_gate=lambda *_args: True,
        now=NOW,
        enabled=True,
    )
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        withdraw_recording_consent(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            source="dtmf",
            now=NOW,
        )
    run_once(
        factory,
        clinic_id="clinic-a",
        worker_id="recording-stop-before-delayed-callback",
        provider=_StaticProvider(
            stop=RecordingProviderResult(
                disposition=RecordingProviderDisposition.ACCEPTED,
                reason=RecordingProviderReason.PROVIDER_ACCEPTED,
                recording_sid=RECORDING_SID,
            )
        ),
        start_gate=lambda *_args: False,
        now=NOW,
        enabled=True,
    )

    with factory.begin() as session:
        receive_twilio_callback(
            session,
            effect_token=start_token,
            callback_kind=ProviderCallbackKind.RECORDING,
            fields={
                "RecordingSid": RECORDING_SID,
                "RecordingStatus": "in-progress",
            },
            raw_payload=b"late-in-progress-after-stop",
            received_at=NOW,
        )

    ledger = _ledger(factory)
    assert ledger.recording_status == CallRecordingStatus.COMPLETED
    assert ledger.recording_stopped_at == NOW


def test_switch_off_cancels_undispatched_recording_start(factory) -> None:
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        effect, _created = request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=ledger.id,
            now=NOW,
        )
        result = enforce_recording_switch_off(
            session,
            clinic_id="clinic-a",
            now=NOW,
            reason_code="recording_switch_disabled",
        )

    with factory() as session:
        ledger = session.query(CallRecord).one()
        effect = session.get(ExternalEffect, effect.id)
        assert result.canceled_starts == 1
        assert result.stops_enqueued == 0
        assert effect is not None and effect.state == ExternalEffectState.CANCELED
        assert ledger.recording_status == CallRecordingStatus.ABSENT


def test_switch_off_enqueues_one_stop_for_active_recording(factory) -> None:
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        ledger.recording_sid = RECORDING_SID
        ledger.recording_status = CallRecordingStatus.IN_PROGRESS
        ledger.recording_started_at = NOW
        first = enforce_recording_switch_off(
            session,
            clinic_id="clinic-a",
            now=NOW,
            reason_code="recording_switch_disabled",
        )
        replay = enforce_recording_switch_off(
            session,
            clinic_id="clinic-a",
            now=NOW,
            reason_code="recording_switch_disabled",
        )

    with factory() as session:
        ledger = session.query(CallRecord).one()
        effect = session.query(ExternalEffect).one()
        assert first.stops_enqueued == 1
        assert replay.stops_enqueued == 0
        assert ledger.recording_status == CallRecordingStatus.STOP_PENDING
        assert effect.payload == {
            "intent": "recording_stop",
            "call_record_id": ledger.id,
        }


def test_runtime_batch_dispatches_stop_when_start_enablement_is_false(
    factory,
    monkeypatch,
) -> None:
    with factory.begin() as session:
        ledger = session.query(CallRecord).one()
        ledger.recording_sid = RECORDING_SID
        ledger.recording_status = CallRecordingStatus.IN_PROGRESS
        ledger.recording_started_at = NOW
    provider = _StaticProvider(
        stop=RecordingProviderResult(
            disposition=RecordingProviderDisposition.ACCEPTED,
            reason=RecordingProviderReason.PROVIDER_ACCEPTED,
            recording_sid=RECORDING_SID,
        )
    )
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_ENABLED", "false")
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_PROVIDER", "twilio")
    monkeypatch.setattr(recording_worker_module, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(recording_worker_module, "_runtime_provider", lambda: provider)

    result = recording_worker_module.run_runtime_batch(
        clinic_id="clinic-a",
        worker_id="recording-switch-off-worker",
        now=NOW,
        limit=10,
    )

    assert result is not None
    assert result.stopped == 1
    assert provider.stop_calls == 1
    ledger = _ledger(factory)
    assert ledger.recording_status == CallRecordingStatus.COMPLETED
    assert ledger.recording_stopped_at == NOW


@pytest.mark.parametrize(
    ("status", "content", "disposition", "reason"),
    [
        (
            201,
            b'{"sid":"REbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","status":"in-progress"}',
            RecordingProviderDisposition.ACCEPTED,
            RecordingProviderReason.PROVIDER_ACCEPTED,
        ),
        (
            201,
            b'{"sid":"REbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}',
            RecordingProviderDisposition.AMBIGUOUS,
            RecordingProviderReason.PROVIDER_STATE_UNCONFIRMED,
        ),
        (
            201,
            b"{}",
            RecordingProviderDisposition.AMBIGUOUS,
            RecordingProviderReason.MISSING_RECORDING_SID,
        ),
        (
            201,
            b"not-json",
            RecordingProviderDisposition.AMBIGUOUS,
            RecordingProviderReason.MALFORMED_RESPONSE,
        ),
        (
            400,
            b'{"message":"raw validation detail"}',
            RecordingProviderDisposition.REJECTED,
            RecordingProviderReason.PROVIDER_REJECTED,
        ),
        (
            500,
            b'{"message":"raw server detail"}',
            RecordingProviderDisposition.AMBIGUOUS,
            RecordingProviderReason.PROVIDER_SERVER_ERROR,
        ),
    ],
)
def test_twilio_start_adapter_returns_closed_minimized_outcomes(
    status,
    content,
    disposition,
    reason,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/2010-04-01/Accounts/AC123/Calls/{CALL_SID}/Recordings.json"
        body = parse_qs(request.content.decode())
        assert body["RecordingChannels"] == ["dual"]
        assert body["RecordingStatusCallbackEvent"] == [
            "in-progress",
            "completed",
            "absent",
        ]
        return httpx.Response(status, content=content)

    provider = TwilioRecordingProvider(
        account_sid="AC123",
        auth_token="synthetic-secret",
        transport=httpx.MockTransport(handler),
    )
    result = provider.start_recording(
        call_sid=CALL_SID,
        callback_url="https://clinic.example.test/api/v1/voice/twilio/recording-status?effect_token=opaque",
    )

    assert result.disposition == disposition
    assert result.reason == reason
    assert "raw" not in str(result)


def test_twilio_stop_adapter_uses_exact_recording_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (
            f"/2010-04-01/Accounts/AC123/Calls/{CALL_SID}/Recordings/" f"{RECORDING_SID}.json"
        )
        assert parse_qs(request.content.decode()) == {"Status": ["stopped"]}
        return httpx.Response(200, json={"sid": RECORDING_SID, "status": "stopped"})

    provider = TwilioRecordingProvider(
        account_sid="AC123",
        auth_token="synthetic-secret",
        transport=httpx.MockTransport(handler),
    )

    result = provider.stop_recording(
        call_sid=CALL_SID,
        recording_sid=RECORDING_SID,
    )

    assert result == RecordingProviderResult(
        disposition=RecordingProviderDisposition.ACCEPTED,
        reason=RecordingProviderReason.PROVIDER_ACCEPTED,
        recording_sid=RECORDING_SID,
        provider_status="stopped",
    )


def test_twilio_stop_adapter_requires_terminal_provider_state() -> None:
    provider = TwilioRecordingProvider(
        account_sid="AC123",
        auth_token="synthetic-secret",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"sid": RECORDING_SID, "status": "in-progress"},
            )
        ),
    )

    result = provider.stop_recording(
        call_sid=CALL_SID,
        recording_sid=RECORDING_SID,
    )

    assert result.disposition == RecordingProviderDisposition.AMBIGUOUS
    assert result.reason == RecordingProviderReason.PROVIDER_STATE_UNCONFIRMED
    assert result.recording_sid is None


def test_twilio_stop_adapter_quarantines_identity_conflict() -> None:
    provider = TwilioRecordingProvider(
        account_sid="AC123",
        auth_token="synthetic-secret",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"sid": "RE" + "d" * 32})
        ),
    )

    result = provider.stop_recording(
        call_sid=CALL_SID,
        recording_sid=RECORDING_SID,
    )

    assert result.disposition == RecordingProviderDisposition.AMBIGUOUS
    assert result.reason == RecordingProviderReason.PROVIDER_IDENTITY_CONFLICT
    assert result.recording_sid is None


def test_twilio_recording_adapter_transport_error_is_minimized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private transport detail", request=request)

    provider = TwilioRecordingProvider(
        account_sid="AC123",
        auth_token="synthetic-secret",
        transport=httpx.MockTransport(handler),
    )

    result = provider.start_recording(
        call_sid=CALL_SID,
        callback_url="https://clinic.example.test/api/v1/voice/twilio/recording-status",
    )

    assert result.disposition == RecordingProviderDisposition.AMBIGUOUS
    assert result.reason == RecordingProviderReason.TRANSPORT_ERROR
    assert "private" not in str(result).lower()


def test_recording_worker_cli_is_fail_closed_without_runtime_authority(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CLINIC_RECALL_DURABLE_RECORDING_ENABLED", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_DURABLE_RECORDING_PROVIDER", raising=False)
    monkeypatch.setattr(
        recording_worker_module,
        "get_sessionmaker",
        lambda: pytest.fail("disabled recording worker opened the database"),
        raising=False,
    )

    assert recording_worker_module.main(["--clinic-id", "clinic-a"]) == 0
    assert capsys.readouterr().out.strip() == (
        '{"canceled": 0, "claimed": 0, "enabled": false, '
        '"reconcile_required": 0, "rejected": 0, "started": 0, "stopped": 0}'
    )

    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_PROVIDER", "acs")
    assert recording_worker_module.main(["--clinic-id", "clinic-a"]) == 2
    capsys.readouterr()

    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_PROVIDER", "twilio")
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    assert recording_worker_module.main(["--clinic-id", "clinic-a"]) == 2


def test_recording_worker_cli_executes_one_finite_enabled_batch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "synthetic-secret")
    observed = {}

    def fake_run_runtime_batch(**kwargs):
        observed.update(kwargs)
        return recording_worker_module.RecordingRunOnceResult(enabled=True, started=1)

    monkeypatch.setattr(
        recording_worker_module,
        "run_runtime_batch",
        fake_run_runtime_batch,
    )

    exit_code = recording_worker_module.main(
        [
            "--clinic-id",
            "clinic-a",
            "--worker-id",
            "recording-worker-test",
            "--limit",
            "4",
            "--now",
            NOW.isoformat(),
        ]
    )

    assert exit_code == 0
    assert observed["clinic_id"] == "clinic-a"
    assert observed["worker_id"] == "recording-worker-test"
    assert observed["limit"] == 4
    assert capsys.readouterr().out.strip() == (
        '{"canceled": 0, "claimed": 0, "enabled": true, '
        '"reconcile_required": 0, "rejected": 0, "started": 1, "stopped": 0}'
    )


def test_runtime_start_gate_requires_current_disclosure_and_active_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(Clinic(id="clinic-runtime-gate", name="Runtime Gate Clinic"))
        session.add(
            PilotProgramme(
                id="pilot-runtime-gate",
                clinic_id="clinic-runtime-gate",
                environment="production",
                release_identity="sha256:runtime-gate",
                state=PilotProgrammeState.ACTIVE,
                active_cumulative_limit=5,
            )
        )
        inbound = InboundCall(
            id="inbound-runtime-gate",
            clinic_id="clinic-runtime-gate",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id="CA" + "6" * 32,
            called_number="+441111111111",
            status=InboundCallStatus.STARTED,
        )
        session.add(inbound)
        session.flush()
        record = ensure_call_record(
            session,
            "clinic-runtime-gate",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=inbound.provider_call_id,
            inbound_call_id=inbound.id,
            session_id="runtime-gate-session",
            direction=InteractionDirection.INBOUND,
            scenario="inbound_clinic",
            patient_id=None,
            consent_snapshot=None,
            now=NOW,
        )
        record.consent_state = RecordingConsentState.GRANTED
        record.consent_version = "synthetic-pr09-v1"
        session.commit()

        monkeypatch.setenv("CLINIC_RECALL_DURABLE_RECORDING_ENABLED", "true")
        monkeypatch.setenv("CLINIC_RECALL_PILOT_OUTREACH_ENABLED", "true")
        monkeypatch.setenv("CLINIC_RECALL_PILOT_VOICE_ENABLED", "true")
        monkeypatch.setenv("CLINIC_RECALL_PILOT_RECORDING_ENABLED", "true")
        monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT", NOW.isoformat())
        monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS", "60")
        monkeypatch.setenv("CLINIC_RECALL_PILOT_ENVIRONMENT", "production")
        monkeypatch.setenv(
            "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
            "sha256:runtime-gate",
        )
        monkeypatch.setenv("CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED", "true")
        monkeypatch.setenv(
            "CLINIC_RECALL_RECORDING_DISCLOSURE_REFRESHED_AT",
            NOW.isoformat(),
        )
        monkeypatch.setenv(
            "CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT",
            "Synthetic recording disclosure for deterministic runtime gate testing.",
        )
        monkeypatch.setenv(
            "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION",
            "synthetic-pr09-v1",
        )

        assert (
            recording_worker_module._runtime_start_gate(
                session,
                "clinic-runtime-gate",
                record,
                NOW,
            )
            is True
        )
        monkeypatch.setenv(
            "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION",
            "synthetic-pr09-v2",
        )
        assert (
            recording_worker_module._runtime_start_gate(
                session,
                "clinic-runtime-gate",
                record,
                NOW,
            )
            is False
        )
        monkeypatch.setenv(
            "CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION",
            "synthetic-pr09-v1",
        )
        inbound.status = InboundCallStatus.COMPLETED
        session.flush()
        assert (
            recording_worker_module._runtime_start_gate(
                session,
                "clinic-runtime-gate",
                record,
                NOW,
            )
            is False
        )
