"""HTTP boundary contracts for signed, minimized provider callbacks."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from apps.artagent.backend.api.v1.endpoints import sms as sms_endpoint
from apps.artagent.backend.api.v1.endpoints import voice as voice_endpoint
from apps.artagent.backend.api.v1.endpoints.sms import _make_twilio_signature
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.durable.callbacks import generate_effect_token
from src.clinic_recall.enums import (
    CallRecordingStatus,
    CampaignStatus,
    CampaignType,
    Channel,
    ClinicPhoneProvider,
    ExternalEffectState,
    ExternalEffectType,
    InteractionDirection,
    OutreachState,
    ProviderCallbackState,
    RecordingConsentState,
)
from src.clinic_recall.models import (
    Base,
    CallRecord,
    Campaign,
    Clinic,
    ExternalEffect,
    OutreachJob,
    Patient,
    ProviderCallbackReceipt,
)
from src.clinic_recall.pilot_controls import (
    create_programme,
    enroll_participant,
    mark_programme_dark,
    release_cumulative_limit,
)
from src.clinic_recall.recording import ensure_call_record

NOW = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
ACCOUNT_SID = "AC" + "c" * 32
MESSAGE_SID = "SM" + "d" * 32
CALL_SID = "CA" + "e" * 32
RECORDING_SID = "RE" + "f" * 32


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(sms_endpoint.router, prefix="/api/v1/sms")
    return app


def _voice_app() -> FastAPI:
    app = FastAPI()
    app.include_router(voice_endpoint.router, prefix="/api/v1/voice")
    return app


def _factory() -> tuple[sessionmaker[Session], str, str]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    clinic_id = "clinic-callback-endpoint-a"
    effect_token = generate_effect_token(clinic_id)
    with factory() as session:
        session.add(Clinic(id=clinic_id, name="Callback Endpoint A"))
        session.add(Clinic(id="clinic-callback-endpoint-b", name="Callback Endpoint B"))
        session.add(
            ExternalEffect(
                id="effect-callback-endpoint-a",
                clinic_id=clinic_id,
                aggregate_type="callback_test",
                aggregate_id="sms-callback-test",
                effect_type=ExternalEffectType.SMS,
                idempotency_key="recall-sms:callback-endpoint-a",
                callback_token=effect_token,
                payload_version=1,
                payload={"intent": "callback_test"},
                request_hash="2" * 64,
                state=ExternalEffectState.DISPATCHING,
                available_at=NOW,
                attempt_count=1,
                max_attempts=1,
                lease_owner="worker-endpoint",
            )
        )
        session.commit()
    return factory, clinic_id, effect_token


def _configure(monkeypatch, factory: sessionmaker[Session]) -> None:
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    monkeypatch.setenv("CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED", "true")
    monkeypatch.setattr(sms_endpoint, "get_sessionmaker", lambda: factory)


def _voice_effect_factory(
    effect_type: ExternalEffectType,
    *,
    suffix: str,
) -> tuple[sessionmaker[Session], str]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    clinic_id = f"clinic-callback-{suffix}"
    effect_token = generate_effect_token(clinic_id)
    with factory() as session:
        session.add(Clinic(id=clinic_id, name=f"Callback {suffix}"))
        session.add(
            ExternalEffect(
                id=f"effect-callback-{suffix}",
                clinic_id=clinic_id,
                aggregate_type="callback_test",
                aggregate_id=f"{suffix}-callback-test",
                effect_type=effect_type,
                idempotency_key=f"recall-{effect_type.value}:{suffix}",
                callback_token=effect_token,
                payload_version=1,
                payload={"intent": "callback_test"},
                request_hash="4" * 64,
                state=ExternalEffectState.DISPATCHING,
                available_at=NOW,
                attempt_count=1,
                max_attempts=1,
                lease_owner=f"worker-{suffix}",
            )
        )
        session.commit()
    return factory, effect_token


def _configure_voice(monkeypatch, factory: sessionmaker[Session]) -> None:
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    monkeypatch.setenv("CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED", "true")
    monkeypatch.setenv(
        "TWILIO_MEDIA_STREAM_URL",
        "wss://clinic.example.test/api/v1/twilio/stream",
    )
    monkeypatch.setenv("CLINIC_RECALL_PILOT_OUTREACH_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_VOICE_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_RECORDING_ENABLED", "false")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT", datetime.now(UTC).isoformat())
    monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS", "60")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_ENVIRONMENT", "production")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_RELEASE_IDENTITY", "sha256:callback-amd")
    monkeypatch.setattr(voice_endpoint, "get_sessionmaker", lambda: factory)


def _signed_post(
    client: TestClient,
    *,
    token: str,
    data: dict[str, str],
    signature_url: str | None = None,
    signature: str | None = None,
):
    path = f"/api/v1/sms/twilio?effect_token={token}"
    public_url = signature_url or f"https://clinic.example.test{path}"
    signed = signature or _make_twilio_signature(public_url, data, "secret")
    return client.post(path, data=data, headers={"X-Twilio-Signature": signed})


def _delivery_form(status: str = "delivered") -> dict[str, str]:
    return {
        "AccountSid": ACCOUNT_SID,
        "MessageSid": MESSAGE_SID,
        "MessageStatus": status,
        "From": "+447700900001",
        "To": "+447700900002",
        "CallToken": '{"alg":"RS256","value":"header.payload.signature"}',
        "FutureProviderField": "signed-but-not-persisted",
    }


def test_signed_duplicate_sms_callback_is_persisted_and_applied_once(
    monkeypatch,
    caplog,
) -> None:
    factory, _clinic_id, effect_token = _factory()
    _configure(monkeypatch, factory)
    client = TestClient(_app())
    data = _delivery_form()

    first = _signed_post(client, token=effect_token, data=data)
    second = _signed_post(client, token=effect_token, data=data)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["content-type"].startswith("application/xml")
    assert second.headers["content-type"].startswith("application/xml")
    with factory() as session:
        receipt = session.execute(select(ProviderCallbackReceipt)).scalar_one()
        effect = session.get(ExternalEffect, "effect-callback-endpoint-a")
        assert receipt.state == ProviderCallbackState.APPLIED
        assert receipt.provider_resource_id == MESSAGE_SID
        assert receipt.normalized_status == "delivered"
        assert len(receipt.payload_hash) == 64
        assert effect is not None
        assert effect.state == ExternalEffectState.SUCCEEDED
        assert effect.provider_status == "delivery_succeeded"
        assert effect.attempt_count == 1
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 1
        serialized = repr(receipt.__dict__)
        assert "+447700" not in serialized
        assert "header.payload.signature" not in serialized
        assert "signed-but-not-persisted" not in serialized
    assert effect_token not in caplog.text
    assert MESSAGE_SID not in caplog.text
    assert "+447700" not in caplog.text
    assert "header.payload.signature" not in caplog.text


def test_callback_application_gate_defaults_off_but_retains_verified_receipt(
    monkeypatch,
) -> None:
    factory, _clinic_id, effect_token = _factory()
    _configure(monkeypatch, factory)
    monkeypatch.delenv("CLINIC_RECALL_CALLBACK_APPLICATION_ENABLED", raising=False)

    response = _signed_post(
        TestClient(_app()),
        token=effect_token,
        data=_delivery_form(),
    )

    assert response.status_code == 200
    with factory() as session:
        receipt = session.execute(select(ProviderCallbackReceipt)).scalar_one()
        effect = session.get(ExternalEffect, "effect-callback-endpoint-a")
        assert receipt.state == ProviderCallbackState.PENDING
        assert effect is not None
        assert effect.state == ExternalEffectState.DISPATCHING
        assert effect.provider_resource_id is None


def test_invalid_signature_does_not_create_receipt_or_mutate_effect(monkeypatch) -> None:
    factory, _clinic_id, effect_token = _factory()
    _configure(monkeypatch, factory)

    response = _signed_post(
        TestClient(_app()),
        token=effect_token,
        data=_delivery_form(),
        signature="invalid",
    )

    assert response.status_code == 401
    with factory() as session:
        effect = session.get(ExternalEffect, "effect-callback-endpoint-a")
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0
        assert effect is not None and effect.state == ExternalEffectState.DISPATCHING


def test_signature_for_one_effect_token_cannot_be_replayed_at_another(
    monkeypatch,
) -> None:
    factory, _clinic_id, effect_token = _factory()
    _configure(monkeypatch, factory)
    other_token = generate_effect_token("clinic-callback-endpoint-a")
    data = _delivery_form()
    signed_url = f"https://clinic.example.test/api/v1/sms/twilio?effect_token={effect_token}"
    signature = _make_twilio_signature(signed_url, data, "secret")

    response = TestClient(_app()).post(
        f"/api/v1/sms/twilio?effect_token={other_token}",
        data=data,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 401
    with factory() as session:
        effect = session.get(ExternalEffect, "effect-callback-endpoint-a")
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0
        assert effect is not None and effect.state == ExternalEffectState.DISPATCHING


def test_durable_callback_requires_signature_even_when_legacy_unsigned_is_enabled(
    monkeypatch,
) -> None:
    factory, _clinic_id, effect_token = _factory()
    _configure(monkeypatch, factory)
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")

    response = TestClient(_app()).post(
        f"/api/v1/sms/twilio?effect_token={effect_token}",
        data=_delivery_form(),
    )

    assert response.status_code == 401
    with factory() as session:
        effect = session.get(ExternalEffect, "effect-callback-endpoint-a")
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0
        assert effect is not None and effect.state == ExternalEffectState.DISPATCHING


def test_durable_callback_rejects_request_url_when_public_base_is_unconfigured(
    monkeypatch,
) -> None:
    factory, _clinic_id, effect_token = _factory()
    _configure(monkeypatch, factory)
    monkeypatch.delenv("TWILIO_WEBHOOK_BASE_URL", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    path = f"/api/v1/sms/twilio?effect_token={effect_token}"
    data = _delivery_form()
    signature = _make_twilio_signature(f"http://testserver{path}", data, "secret")

    response = TestClient(_app()).post(
        path,
        data=data,
        headers={
            "X-Twilio-Signature": signature,
            "X-Forwarded-Host": "clinic.example.test",
            "X-Forwarded-Proto": "https",
        },
    )

    assert response.status_code == 401
    with factory() as session:
        effect = session.get(ExternalEffect, "effect-callback-endpoint-a")
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0
        assert effect is not None and effect.state == ExternalEffectState.DISPATCHING


def test_unknown_cross_tenant_or_malformed_token_does_not_mutate_effect(
    monkeypatch,
) -> None:
    factory, _clinic_id, _effect_token = _factory()
    _configure(monkeypatch, factory)
    client = TestClient(_app())
    tokens = (
        generate_effect_token("clinic-callback-endpoint-a"),
        generate_effect_token("clinic-callback-endpoint-b"),
        "malformed-token",
    )

    statuses = [
        _signed_post(client, token=token, data=_delivery_form()).status_code for token in tokens
    ]

    assert statuses == [404, 404, 400]
    with factory() as session:
        effect = session.get(ExternalEffect, "effect-callback-endpoint-a")
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0
        assert effect is not None and effect.state == ExternalEffectState.DISPATCHING


def test_unknown_sms_status_returns_safe_error_without_mutation(monkeypatch) -> None:
    factory, _clinic_id, effect_token = _factory()
    _configure(monkeypatch, factory)

    response = _signed_post(
        TestClient(_app()),
        token=effect_token,
        data=_delivery_form("invented"),
    )

    assert response.status_code == 400
    assert MESSAGE_SID not in response.text
    assert effect_token not in response.text
    with factory() as session:
        effect = session.get(ExternalEffect, "effect-callback-endpoint-a")
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0
        assert effect is not None and effect.state == ExternalEffectState.DISPATCHING


def test_signed_voice_status_callback_is_persisted_by_sequence(monkeypatch) -> None:
    factory, effect_token = _voice_effect_factory(ExternalEffectType.CALL, suffix="voice")
    _configure_voice(monkeypatch, factory)
    with factory.begin() as session:
        effect = session.get(ExternalEffect, "effect-callback-voice")
        assert effect is not None
        ensure_call_record(
            session,
            "clinic-callback-voice",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=CALL_SID,
            external_effect_id=effect.id,
            session_id="voice-status-ledger",
            direction=InteractionDirection.OUTBOUND,
            scenario="rebooking",
            patient_id=None,
            consent_snapshot=None,
            now=NOW,
        )
    path = f"/api/v1/voice/twilio/call-status?effect_token={effect_token}"
    data = {
        "AccountSid": ACCOUNT_SID,
        "CallSid": CALL_SID,
        "CallStatus": "completed",
        "SequenceNumber": "2",
        "Timestamp": "Sun, 19 Jul 2026 10:02:00 +0000",
        "FutureProviderField": "signature-only",
    }
    signature = _make_twilio_signature(
        f"https://clinic.example.test{path}",
        data,
        "secret",
    )

    response = TestClient(_voice_app()).post(
        path,
        data=data,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    with factory() as session:
        receipt = session.execute(select(ProviderCallbackReceipt)).scalar_one()
        effect = session.get(ExternalEffect, "effect-callback-voice")
        assert receipt.callback_kind.value == "voice"
        assert receipt.provider_sequence == 2
        assert effect is not None and effect.provider_sequence == 2
        assert "signature-only" not in repr(receipt.__dict__)
        ledger = session.execute(select(CallRecord)).scalar_one()
        assert ledger.ended_at is not None


def test_signed_recording_status_callback_uses_recording_contract(monkeypatch) -> None:
    factory, effect_token = _voice_effect_factory(
        ExternalEffectType.RECORDING,
        suffix="recording",
    )
    _configure_voice(monkeypatch, factory)
    with factory() as session:
        record = ensure_call_record(
            session,
            "clinic-callback-recording",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=CALL_SID,
            session_id="recording-callback-session",
            direction=InteractionDirection.OUTBOUND,
            scenario="rebooking",
            patient_id=None,
            consent_snapshot=None,
            now=NOW,
        )
        record.consent_state = RecordingConsentState.GRANTED
        record.consent_version = "synthetic-pr09-v1"
        record.recording_requested_at = NOW
        record.recording_status = CallRecordingStatus.START_PENDING
        effect = session.get(ExternalEffect, "effect-callback-recording")
        assert effect is not None
        effect.aggregate_type = "call_record"
        effect.aggregate_id = record.id
        effect.payload = {
            "intent": "recording_start",
            "call_record_id": record.id,
        }
        session.commit()
    path = f"/api/v1/voice/twilio/recording-status?effect_token={effect_token}"
    data = {
        "AccountSid": ACCOUNT_SID,
        "CallSid": CALL_SID,
        "RecordingSid": RECORDING_SID,
        "RecordingStatus": "in-progress",
        "RecordingUrl": "https://provider.invalid/sensitive-recording-url",
    }
    signature = _make_twilio_signature(
        f"https://clinic.example.test{path}",
        data,
        "secret",
    )

    response = TestClient(_voice_app()).post(
        path,
        data=data,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    with factory() as session:
        receipt = session.execute(select(ProviderCallbackReceipt)).scalar_one()
        assert receipt.callback_kind.value == "recording"
        assert receipt.provider_resource_id == RECORDING_SID
        assert "sensitive-recording-url" not in repr(receipt.__dict__)


def test_recording_callback_cannot_cross_clinic_call_record(monkeypatch) -> None:
    factory, effect_token = _voice_effect_factory(
        ExternalEffectType.RECORDING,
        suffix="recording",
    )
    _configure_voice(monkeypatch, factory)
    with factory() as session:
        session.add(Clinic(id="clinic-recording-other", name="Other Recording Clinic"))
        ensure_call_record(
            session,
            "clinic-recording-other",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=CALL_SID,
            session_id="other-clinic-session",
            direction=InteractionDirection.OUTBOUND,
            scenario="rebooking",
            patient_id=None,
            consent_snapshot={"record_call": True},
            now=NOW,
        )
        session.commit()
    path = f"/api/v1/voice/twilio/recording-status?effect_token={effect_token}"
    data = {
        "AccountSid": ACCOUNT_SID,
        "CallSid": CALL_SID,
        "RecordingSid": RECORDING_SID,
        "RecordingStatus": "in-progress",
    }
    signature = _make_twilio_signature(
        f"https://clinic.example.test{path}",
        data,
        "secret",
    )

    response = TestClient(_voice_app()).post(
        path,
        data=data,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 404
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0
        effect = session.get(ExternalEffect, "effect-callback-recording")
        other_record = session.execute(
            select(CallRecord).where(CallRecord.clinic_id == "clinic-recording-other")
        ).scalar_one()
        assert effect is not None and effect.state == ExternalEffectState.DISPATCHING
        assert other_record.recording_sid is None


def test_recording_callback_rejects_off_provider_url_before_durable_mutation(
    monkeypatch,
) -> None:
    factory, effect_token = _voice_effect_factory(
        ExternalEffectType.RECORDING,
        suffix="recording",
    )
    _configure_voice(monkeypatch, factory)
    with factory() as session:
        ensure_call_record(
            session,
            "clinic-callback-recording",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=CALL_SID,
            session_id="recording-url-session",
            direction=InteractionDirection.OUTBOUND,
            scenario="rebooking",
            patient_id=None,
            consent_snapshot={"record_call": True},
            now=NOW,
        )
        session.commit()
    monkeypatch.setattr(
        voice_endpoint,
        "_download_twilio_recording",
        lambda *_args: pytest.fail("off-provider URL reached the downloader"),
    )
    path = f"/api/v1/voice/twilio/recording-status?effect_token={effect_token}"
    data = {
        "AccountSid": ACCOUNT_SID,
        "CallSid": CALL_SID,
        "RecordingSid": RECORDING_SID,
        "RecordingStatus": "completed",
        "RecordingUrl": "https://attacker.invalid/internal-metadata",
    }
    signature = _make_twilio_signature(
        f"https://clinic.example.test{path}",
        data,
        "secret",
    )

    response = TestClient(_voice_app()).post(
        path,
        data=data,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 400
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ProviderCallbackReceipt)) == 0
        effect = session.get(ExternalEffect, "effect-callback-recording")
        assert effect is not None and effect.state == ExternalEffectState.DISPATCHING


def test_signed_synchronous_amd_is_persisted_without_forwarding_token(
    monkeypatch,
    caplog,
) -> None:
    factory, effect_token = _voice_effect_factory(ExternalEffectType.CALL, suffix="amd")
    _configure_voice(monkeypatch, factory)
    with factory.begin() as session:
        session.add(
            Patient(
                id="patient-callback-amd",
                clinic_id="clinic-callback-amd",
                source_ref="patient-callback-amd",
                name="Synthetic Callback AMD Patient",
                phone="+447700900101",
                consent_flags={"call": True},
                opt_out_flags={},
            )
        )
        session.add(
            Campaign(
                id="campaign-callback-amd",
                clinic_id="clinic-callback-amd",
                type=CampaignType.RECOVERY,
                status=CampaignStatus.ACTIVE,
            )
        )
        session.add(
            OutreachJob(
                id="job-callback-amd",
                clinic_id="clinic-callback-amd",
                campaign_id="campaign-callback-amd",
                patient_id="patient-callback-amd",
                channel=Channel.SMS,
                state=OutreachState.NO_REPLY,
            )
        )
        programme = create_programme(
            session,
            clinic_id="clinic-callback-amd",
            programme_id="pilot-callback-amd",
            environment="production",
            release_identity="sha256:callback-amd",
        )
        mark_programme_dark(
            session,
            clinic_id="clinic-callback-amd",
            programme_id=programme.id,
            actor="operator:test",
            evidence_hash="d" * 64,
            now=NOW,
        )
        for ordinal in range(2, 6):
            session.add(
                Patient(
                    id=f"patient-callback-amd-{ordinal}",
                    clinic_id="clinic-callback-amd",
                    source_ref=f"patient-callback-amd-{ordinal}",
                    name=f"Synthetic Callback AMD {ordinal}",
                )
            )
        session.flush()
        for patient_id in ["patient-callback-amd"] + [
            f"patient-callback-amd-{ordinal}" for ordinal in range(2, 6)
        ]:
            enroll_participant(
                session,
                clinic_id="clinic-callback-amd",
                programme_id=programme.id,
                patient_id=patient_id,
                now=NOW,
            )
        release_cumulative_limit(
            session,
            clinic_id="clinic-callback-amd",
            programme_id=programme.id,
            cumulative_limit=5,
            actor="operator:test",
            evidence_hash="a" * 64,
            now=NOW,
        )
        effect = session.get(ExternalEffect, "effect-callback-amd")
        assert effect is not None
        effect.aggregate_type = "outreach_job"
        effect.aggregate_id = "job-callback-amd"
        effect.payload = {
            "intent": "recall_fallback",
            "outreach_job_id": "job-callback-amd",
        }
    query = urlencode(
        {
            "source": "clinic_recall_voice_worker",
            "scenario": "rebooking",
            "clinic_id": "clinic-callback-amd",
            "patient_id": "patient-callback-amd",
            "outreach_job_id": "job-callback-amd",
            "record_call": "false",
            "effect_token": effect_token,
        }
    )
    path = f"/api/v1/voice/twilio/twiml?{query}"
    data = {
        "AccountSid": ACCOUNT_SID,
        "CallSid": CALL_SID,
        "AnsweredBy": "human",
        "CallToken": '{"parentCallInfoToken":"fixture","identityHeaderTokens":[]}',
    }
    signature = _make_twilio_signature(
        f"https://clinic.example.test{path}",
        data,
        "secret",
    )

    response = TestClient(_voice_app()).post(
        path,
        data=data,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert "<Connect>" in response.text
    assert effect_token not in response.text
    with factory() as session:
        receipt = session.execute(select(ProviderCallbackReceipt)).scalar_one()
        assert receipt.callback_kind.value == "amd"
        assert receipt.normalized_status == "human"
        assert "eyJhbGciOiJSUzI1NiJ9" not in repr(receipt.__dict__)
    assert "eyJhbGciOiJSUzI1NiJ9" not in caplog.text
    assert effect_token not in caplog.text
