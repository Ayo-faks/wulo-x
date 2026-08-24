"""HTTP tests for the Twilio recording-status webhook pipeline."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from apps.artagent.backend.api.v1.endpoints import voice
from apps.artagent.backend.api.v1.endpoints.sms import _make_twilio_signature
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.enums import (
    CallRecordingStatus,
    ClinicPhoneProvider,
    ExternalEffectState,
    InteractionDirection,
    RecordingConsentState,
)
from src.clinic_recall.models import Base, CallRecord, Clinic
from src.clinic_recall.recording import ensure_call_record, request_recording_start

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
CALL_SID = "CA" + "1" * 32
RECORDING_SID = "RE" + "2" * 32


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(voice.router, prefix="/api/v1/voice")
    return app


def _factory() -> tuple[sessionmaker[Session], str]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Clinic(id="clinic-a", name="Clinic A"))
        record = ensure_call_record(
            session,
            "clinic-a",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=CALL_SID,
            session_id="twilio-session-1",
            direction=InteractionDirection.OUTBOUND,
            scenario="rebooking",
            patient_id=None,
            consent_snapshot=None,
            now=NOW,
        )
        record.consent_state = RecordingConsentState.GRANTED
        record.consent_version = "synthetic-pr09-v1"
        effect, _created = request_recording_start(
            session,
            clinic_id="clinic-a",
            call_record_id=record.id,
            now=NOW,
        )
        effect.state = ExternalEffectState.DISPATCHING
        effect.attempt_count = 1
        effect.lease_owner = "recording-status-test"
        session.commit()
        return factory, effect.callback_token


class _FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.uploads: list[tuple[str, bytes]] = []

    def upload(self, blob_path: str, data: bytes, **kwargs) -> str:
        from src.clinic_recall.recording import RecordingStoreError

        if self.fail:
            raise RecordingStoreError("recording_upload_failed:Boom")
        self.uploads.append((blob_path, data))
        return blob_path


@pytest.fixture
def harness(monkeypatch):
    factory, effect_token = _factory()
    store = _FakeStore()
    deletes: list[str] = []

    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    monkeypatch.setenv("CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED", "true")
    monkeypatch.setattr(voice, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(voice, "_recording_store", lambda: store)
    monkeypatch.setattr(
        voice, "_download_twilio_recording", lambda url, sid, token: b"wav-bytes"
    )
    monkeypatch.setattr(
        voice,
        "_delete_twilio_recording",
        lambda rec_sid, sid, token: deletes.append(rec_sid) or True,
    )
    return factory, store, deletes, effect_token


_COMPLETED_FORM = {
    "AccountSid": "AC123",
    "CallSid": CALL_SID,
    "RecordingSid": RECORDING_SID,
    "RecordingUrl": (
        "https://api.twilio.com/2010-04-01/Accounts/AC123/Recordings/"
        f"{RECORDING_SID}"
    ),
    "RecordingStatus": "completed",
    "RecordingDuration": "58",
}


def _signed_recording_post(
    client: TestClient,
    path: str,
    data: dict[str, str],
):
    signature = _make_twilio_signature(
        f"https://clinic.example.test{path}",
        data,
        "secret",
    )
    return client.post(path, data=data, headers={"X-Twilio-Signature": signature})


def test_completed_recording_is_stored_and_deleted_from_twilio(harness) -> None:
    factory, store, deletes, effect_token = harness
    client = TestClient(_app())

    path = f"/api/v1/voice/twilio/recording-status?effect_token={effect_token}"
    response = _signed_recording_post(
        client,
        path,
        _COMPLETED_FORM,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "stored", "twilio_deleted": True}
    assert store.uploads == [
        (f"clinic-a/{CALL_SID}/{RECORDING_SID}.wav", b"wav-bytes")
    ]
    assert deletes == [RECORDING_SID]
    with factory() as session:
        record = session.execute(select(CallRecord)).scalar_one()
        assert record.recording_status == CallRecordingStatus.STORED
        assert record.recording_blob_path == f"clinic-a/{CALL_SID}/{RECORDING_SID}.wav"
        assert record.recording_sid == RECORDING_SID
        assert record.recording_duration_s == 58


def test_duplicate_completed_callback_does_not_reupload_stored_media(harness) -> None:
    _factory_, store, deletes, effect_token = harness
    client = TestClient(_app())
    path = f"/api/v1/voice/twilio/recording-status?effect_token={effect_token}"

    first = _signed_recording_post(client, path, _COMPLETED_FORM)
    replay = _signed_recording_post(client, path, _COMPLETED_FORM)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == {"status": "stored", "twilio_deleted": True}
    assert store.uploads == [
        (f"clinic-a/{CALL_SID}/{RECORDING_SID}.wav", b"wav-bytes")
    ]
    assert deletes == [RECORDING_SID, RECORDING_SID]


def test_provider_copy_deletion_failure_returns_retry_without_reupload(
    harness,
    monkeypatch,
) -> None:
    _factory_, store, _deletes, effect_token = harness
    attempts = 0

    def delete(*_args):
        nonlocal attempts
        attempts += 1
        return attempts > 1

    monkeypatch.setattr(voice, "_delete_twilio_recording", delete)
    client = TestClient(_app())
    path = f"/api/v1/voice/twilio/recording-status?effect_token={effect_token}"

    first = _signed_recording_post(client, path, _COMPLETED_FORM)
    replay = _signed_recording_post(client, path, _COMPLETED_FORM)

    assert first.status_code == 500
    assert first.json() == {"status": "retry", "twilio_deleted": False}
    assert replay.status_code == 200
    assert replay.json() == {"status": "stored", "twilio_deleted": True}
    assert len(store.uploads) == 1
    assert attempts == 2


def test_unknown_call_is_not_stored(harness) -> None:
    _factory_, store, deletes, effect_token = harness
    client = TestClient(_app())

    path = f"/api/v1/voice/twilio/recording-status?effect_token={effect_token}"
    response = _signed_recording_post(
        client,
        path,
        {**_COMPLETED_FORM, "CallSid": "CA" + "9" * 32},
    )

    assert response.status_code == 404
    assert response.json()["status"] == "rejected"
    assert store.uploads == []
    assert deletes == []


def test_tokenless_completed_callback_cannot_store_all_call_ledger(harness) -> None:
    _factory_, store, deletes, _effect_token = harness
    client = TestClient(_app())

    response = client.post(
        "/api/v1/voice/twilio/recording-status",
        data=_COMPLETED_FORM,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "no_call_record"
    assert store.uploads == []
    assert deletes == []


def test_store_failure_returns_500_for_twilio_retry(harness, monkeypatch) -> None:
    factory, _store, deletes, effect_token = harness
    failing = _FakeStore(fail=True)
    monkeypatch.setattr(voice, "_recording_store", lambda: failing)
    client = TestClient(_app())

    path = f"/api/v1/voice/twilio/recording-status?effect_token={effect_token}"
    response = _signed_recording_post(
        client,
        path,
        _COMPLETED_FORM,
    )

    assert response.status_code == 500
    assert deletes == []  # Twilio copy is preserved for the retry
    with factory() as session:
        record = session.execute(select(CallRecord)).scalar_one()
        assert record.recording_status == CallRecordingStatus.FAILED
        assert record.recording_blob_path is None


def test_non_completed_status_is_ignored(harness) -> None:
    _factory_, store, deletes, effect_token = harness
    client = TestClient(_app())

    path = f"/api/v1/voice/twilio/recording-status?effect_token={effect_token}"
    response = _signed_recording_post(
        client,
        path,
        {**_COMPLETED_FORM, "RecordingStatus": "in-progress"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert store.uploads == []
    assert deletes == []


def test_unsigned_callback_rejected_when_signatures_required(harness, monkeypatch) -> None:
    _factory_, _store, _deletes, effect_token = harness
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    client = TestClient(_app())

    response = client.post(
        f"/api/v1/voice/twilio/recording-status?effect_token={effect_token}",
        data=_COMPLETED_FORM,
    )

    assert response.status_code == 401


def test_wrong_account_sid_rejected(harness) -> None:
    _factory_, _store, _deletes, effect_token = harness
    client = TestClient(_app())

    response = client.post(
        f"/api/v1/voice/twilio/recording-status?effect_token={effect_token}",
        data={**_COMPLETED_FORM, "AccountSid": "AC-attacker"},
    )

    assert response.status_code == 403


def test_call_status_emits_aggregate_disposition_without_identifiers(
    harness, monkeypatch
) -> None:
    _factory_, _store, _deletes, _effect_token = harness
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        voice,
        "emit_runtime_event",
        lambda name, attributes: events.append((name, dict(attributes))) or True,
    )
    response = TestClient(_app()).post(
        "/api/v1/voice/twilio/call-status",
        data={
            "AccountSid": "AC123",
            "CallSid": "CA-patient-linked-id",
            "To": "+447700900001",
            "From": "+447700900002",
            "CallStatus": "no-answer",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert events == [
        (
            "voice.call.status",
            {
                "provider": "twilio",
                "status": "no-answer",
                "answered": False,
                "terminal": True,
            },
        )
    ]
    assert "CA-patient-linked-id" not in str(events)
    assert "+447700" not in str(events)


def test_call_status_rejects_invalid_signature(harness, monkeypatch) -> None:
    _factory_, _store, _deletes, _effect_token = harness
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    data = {
        "AccountSid": "AC123",
        "CallSid": "CA123",
        "CallStatus": "completed",
    }
    valid_signature = _make_twilio_signature(
        "https://clinic.example.test/api/v1/voice/twilio/call-status",
        data,
        "secret",
    )
    client = TestClient(_app())

    assert client.post(
        "/api/v1/voice/twilio/call-status",
        data=data,
        headers={"X-Twilio-Signature": "invalid"},
    ).status_code == 401
    assert client.post(
        "/api/v1/voice/twilio/call-status",
        data=data,
        headers={"X-Twilio-Signature": valid_signature},
    ).status_code == 200


def test_signed_tokenless_terminal_status_finalizes_call_once(harness) -> None:
    factory, _store, _deletes, _effect_token = harness
    path = "/api/v1/voice/twilio/call-status"
    data = {
        "AccountSid": "AC123",
        "CallSid": CALL_SID,
        "CallStatus": "completed",
    }
    signature = _make_twilio_signature(
        f"https://clinic.example.test{path}",
        data,
        "secret",
    )
    client = TestClient(_app())

    first = client.post(path, data=data, headers={"X-Twilio-Signature": signature})
    with factory() as session:
        first_ended_at = session.execute(select(CallRecord.ended_at)).scalar_one()
    replay = client.post(path, data=data, headers={"X-Twilio-Signature": signature})

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first_ended_at is not None
    with factory() as session:
        assert session.execute(select(CallRecord.ended_at)).scalar_one() == first_ended_at


def test_hosted_twiml_keeps_clinic_recall_unrecorded_and_generic_compatible(monkeypatch) -> None:
    from src.clinic_recall.voice_worker import RECORDING_ANNOUNCEMENT

    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setenv("TWILIO_MEDIA_STREAM_URL", "wss://clinic.example.test/api/v1/twilio/stream")
    client = TestClient(_app())

    recorded = client.get(
        "/api/v1/voice/twilio/twiml",
        params={
            "session_id": "twilio-session-1",
            "source": "non_durable_recording_compatibility",
            "scenario": "customer_support",
            "clinic_id": "clinic-a",
            "record_call": "true",
        },
    )
    assert recorded.status_code == 200
    assert f"<Say>{RECORDING_ANNOUNCEMENT}</Say><Connect>" in recorded.text

    unrecorded = client.get(
        "/api/v1/voice/twilio/twiml",
        params={
            "session_id": "twilio-session-2",
            "source": "non_durable_recording_compatibility",
            "scenario": "rebooking",
            "clinic_id": "clinic-a",
            "record_call": "true",
        },
    )
    assert unrecorded.status_code == 200
    assert "<Say>" not in unrecorded.text
    assert '<Parameter name="record_call" value="false" />' in unrecorded.text
