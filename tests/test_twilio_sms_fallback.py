import base64
import hashlib
import hmac

import httpx
import pytest
from apps.artagent.backend.api.v1.endpoints import sms as sms_endpoint
from apps.artagent.backend.api.v1.endpoints.sms import (
    _make_twilio_signature,
    _summarize_twilio_sms_form,
    _validate_twilio_signature,
    router,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.acs.sms_service import SmsService
from src.clinic_recall.durable.callbacks import generate_effect_token
from twilio.request_validator import RequestValidator


def _sms_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/sms")
    return app


def test_twilio_signature_validation_accepts_and_rejects_form_webhook() -> None:
    url = "https://clinic.example.test/api/v1/sms/twilio"
    params = {
        "AccountSid": "AC123",
        "From": "+447700900001",
        "To": "+447700900002",
        "Body": "YES please",
        "MessageSid": "SM123",
    }
    token = "secret"
    signature = _make_twilio_signature(url, params, token)

    assert _validate_twilio_signature(url, params, signature, token)
    assert not _validate_twilio_signature(url, {**params, "Body": "changed"}, signature, token)
    assert not _validate_twilio_signature(url, params, "", token)


def test_twilio_signature_matches_documented_hmac_sha1_algorithm() -> None:
    url = "https://clinic.example.test/api/v1/sms/twilio"
    params = {"From": "+1", "Body": "hello"}
    payload = url + "BodyhelloFrom+1"
    expected = base64.b64encode(
        hmac.new(b"token", payload.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")

    assert _make_twilio_signature(url, params, "token") == expected


def test_summarizes_twilio_sms_without_message_content() -> None:
    summary = _summarize_twilio_sms_form(
        {
            "AccountSid": "AC123",
            "MessageSid": "SM123",
            "From": "+447700900001",
            "To": "+447700900002",
            "Body": "TEST reply",
            "SmsStatus": "received",
        }
    )

    assert summary == {
        "event_type": "twilio.sms.received",
        "provider": "twilio",
        "event_id": "SET",
        "account_sid": "SET",
        # Privacy: raw phone numbers are never logged; only presence markers.
        "from": "SET",
        "to": "SET",
        "message_length": 10,
        "status": "received",
    }
    assert "Body" not in summary
    assert "message" not in summary


def test_twilio_sms_webhook_validates_signature(monkeypatch) -> None:
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    monkeypatch.setattr(sms_endpoint, "_route_clinic_recall_sms_reply", lambda **_kwargs: None)
    app = _sms_test_app()
    client = TestClient(app)
    data = {
        "AccountSid": "AC123",
        "MessageSid": "SM123",
        "From": "+447700900001",
        "To": "+447700900002",
        "Body": "YES please",
        "SmsStatus": "received",
    }
    signature = _make_twilio_signature(
        "https://clinic.example.test/api/v1/sms/twilio", data, "secret"
    )

    response = client.post(
        "/api/v1/sms/twilio",
        data=data,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "<Response" in response.text


def test_twilio_sms_webhook_rejects_bad_signature(monkeypatch) -> None:
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    app = _sms_test_app()
    client = TestClient(app)

    response = client.post(
        "/api/v1/sms/twilio",
        data={"AccountSid": "AC123", "Body": "hello"},
        headers={"X-Twilio-Signature": "bad"},
    )

    assert response.status_code == 401


def test_twilio_signature_uses_exact_public_url_query_and_all_provider_fields(
    monkeypatch,
) -> None:
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    monkeypatch.setattr(sms_endpoint, "_route_clinic_recall_sms_reply", lambda **_kwargs: None)
    effect_token = generate_effect_token("clinic-signature-test")
    path = f"/api/v1/sms/twilio?effect_token={effect_token}"
    public_url = f"https://clinic.example.test{path}"
    data = {
        "AccountSid": "AC123",
        "MessageSid": "SM" + "a" * 32,
        "From": "+447700900001",
        "To": "+447700900002",
        "Body": "hello",
        "CallToken": '{"alg":"RS256","payload":"header.payload.signature"}',
        "FutureProviderField": "included-by-sdk",
    }
    validator = RequestValidator("secret")
    signature = validator.compute_signature(public_url, data)

    client = TestClient(_sms_test_app())
    accepted = client.post(
        path,
        data=data,
        headers={
            "X-Twilio-Signature": signature,
            "X-Forwarded-Host": "attacker.invalid",
            "X-Forwarded-Proto": "http",
        },
    )
    wrong_url_signature = validator.compute_signature(
        "https://internal.example.test/api/v1/sms/twilio",
        data,
    )
    rejected = client.post(
        path,
        data=data,
        headers={"X-Twilio-Signature": wrong_url_signature},
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 401


@pytest.mark.parametrize("query", ["", "?effect_token=invalid"])
def test_twilio_delivery_callback_without_valid_effect_token_fails_closed(
    monkeypatch,
    query: str,
) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        sms_endpoint,
        "emit_runtime_event",
        lambda name, attributes: events.append((name, dict(attributes))) or True,
    )
    monkeypatch.setattr(
        sms_endpoint,
        "get_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("database access must not occur")),
    )
    monkeypatch.delenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_WEBHOOK_BASE_URL", "https://clinic.example.test")
    data = {
        "AccountSid": "AC123",
        "MessageSid": "SM-patient-linked-id",
        "From": "+447700900001",
        "To": "+447700900002",
        "MessageStatus": "delivered",
    }
    signature = _make_twilio_signature(
        f"https://clinic.example.test/api/v1/sms/twilio{query}", data, "secret"
    )

    response = TestClient(_sms_test_app()).post(
        f"/api/v1/sms/twilio{query}",
        data=data,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 400
    assert events == []
    assert "SM-patient-linked-id" not in str(events)
    assert "+447700" not in str(events)


async def test_sms_service_sends_via_twilio_when_selected(monkeypatch) -> None:
    monkeypatch.setenv("SMS_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_FROM_PHONE_NUMBER", "+447700900002")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/2010-04-01/Accounts/AC123/Messages.json"
        body = request.content.decode("utf-8")
        assert "From=%2B447700900002" in body
        assert "To=%2B447700900001" in body
        assert "Body=Clinic+Recall+test" in body
        assert (
            "StatusCallback=https%3A%2F%2Fclinic.example.test%2Fapi%2Fv1%2Fsms%2Ftwilio%3F"
            "effect_token%3Dopaque-test-token" in body
        )
        assert request.headers.get("Authorization", "").startswith("Basic ")
        return httpx.Response(201, json={"sid": "SM123", "status": "queued"})

    transport = httpx.MockTransport(handler)
    original_async_client = httpx.AsyncClient

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.client = original_async_client(transport=transport)

        async def __aenter__(self):
            return self.client

        async def __aexit__(self, exc_type, exc, tb):
            await self.client.aclose()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    result = await SmsService().send_sms(
        "+447700900001",
        "Clinic Recall test",
        status_callback_url=(
            "https://clinic.example.test/api/v1/sms/twilio?effect_token=opaque-test-token"
        ),
    )

    assert result["success"] is True
    assert result["provider"] == "twilio"
    assert result["sent_messages"][0]["message_id"] == "SM123"


async def test_sms_service_marks_transport_exception_outcome_unknown(
    monkeypatch,
    caplog,
) -> None:
    target = "+447700900001"
    monkeypatch.setenv("SMS_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("TWILIO_FROM_PHONE_NUMBER", "+447700900002")

    async def raise_timeout(**_kwargs):
        raise TimeoutError(f"synthetic timeout for {target}")

    service = SmsService()
    monkeypatch.setattr(service, "_send_twilio_sms", raise_timeout)

    result = await service.send_sms(target, "Clinic Recall test")

    assert result == {
        "success": False,
        "error": "provider_outcome_unknown",
        "error_class": "TimeoutError",
        "outcome_unknown": True,
        "sent_messages": [],
        "failed_messages": [],
    }
    assert target not in caplog.text
