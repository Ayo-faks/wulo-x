"""HTTP tests for the public demo endpoints (browser session + phone call)."""

from __future__ import annotations

import pytest
from apps.artagent.backend.api.v1.endpoints import demo
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.clinic_recall.demo_gate import DemoGateError, issue_demo_token, verify_demo_token
from src.clinic_recall.voice_worker import CallInitiationResult

SECRET = "endpoint-test-secret"


class _FakePipeline:
    def __init__(self, store: dict[str, int]) -> None:
        self._store = store
        self._ops: list[str] = []

    def incr(self, key: str):
        self._ops.append(key)
        return self

    def expire(self, key: str, ttl, nx=False):
        return self

    def execute(self):
        key = self._ops.pop(0)
        self._store[key] = self._store.get(key, 0) + 1
        return [self._store[key], True]


class _FakeRedisClient:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    def pipeline(self):
        return _FakePipeline(self.store)


class _FakeRedisManager:
    def __init__(self) -> None:
        self.redis_client = _FakeRedisClient()


def _client(monkeypatch, *, browser=True, phone=True, turnstile_ok=True) -> TestClient:
    monkeypatch.setenv("DEMO_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("DEMO_BROWSER_ENABLED", "true" if browser else "false")
    monkeypatch.setenv("DEMO_PHONE_ENABLED", "true" if phone else "false")
    monkeypatch.setenv("DEMO_MAX_SECONDS", "60")
    # Deterministic baseline regardless of ambient shell configuration.
    monkeypatch.delenv("DEMO_EXPERIENCE", raising=False)
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)

    def fake_turnstile(token, remote_ip, **kwargs):
        if not turnstile_ok:
            raise DemoGateError("captcha_failed", status_code=403)

    monkeypatch.setattr(demo, "verify_turnstile", fake_turnstile)

    app = FastAPI()
    app.include_router(demo.router, prefix="/api/v1/demo")
    app.state.redis = _FakeRedisManager()
    return TestClient(app)


_VALID = {
    "work_email": "owner@clinic.example.com",
    "clinic_name": "Riverside Physio",
    "turnstile_token": "tok",
}


def test_demo_session_issues_browser_token(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post("/api/v1/demo/session", json=_VALID)
    assert response.status_code == 200
    body = response.json()
    assert body["max_demo_seconds"] == 60
    payload = verify_demo_token(body["demo_token"], expected_kind="browser", secret=SECRET)
    assert payload["clinic_name"] == "Riverside Physio"


def test_demo_session_disabled_flag_blocks(monkeypatch) -> None:
    client = _client(monkeypatch, browser=False)
    response = client.post("/api/v1/demo/session", json=_VALID)
    assert response.status_code == 403
    assert response.json()["detail"] == "demo_disabled"


def test_demo_session_rejects_failed_captcha(monkeypatch) -> None:
    client = _client(monkeypatch, turnstile_ok=False)
    response = client.post("/api/v1/demo/session", json=_VALID)
    assert response.status_code == 403
    assert response.json()["detail"] == "captcha_failed"


def test_demo_session_rejects_invalid_email(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post("/api/v1/demo/session", json={**_VALID, "work_email": "nope"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_email"


def test_demo_session_rate_limits_by_ip(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_RATE_PER_IP_PER_HOUR", "2")
    client = _client(monkeypatch)
    assert client.post("/api/v1/demo/session", json=_VALID).status_code == 200
    assert client.post("/api/v1/demo/session", json=_VALID).status_code == 200
    response = client.post("/api/v1/demo/session", json=_VALID)
    assert response.status_code == 429


def test_demo_session_fails_closed_without_redis(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.app.state.redis = None
    response = client.post("/api/v1/demo/session", json=_VALID)
    assert response.status_code == 503
    assert response.json()["detail"] == "rate_limiter_unavailable"


# ---------------------------------------------------------------------------
# Phone demo call
# ---------------------------------------------------------------------------


class _CapturedInitiator:
    """Stands in for TwilioCallInitiator; records the initiation request."""

    calls: list[dict] = []
    result = CallInitiationResult(successful=True, call_id="CA-demo", provider="twilio")

    def initiate_call(self, *, target_number: str, context: dict) -> CallInitiationResult:
        type(self).calls.append({"target_number": target_number, "context": context})
        return type(self).result


@pytest.fixture(autouse=True)
def _reset_captured() -> None:
    _CapturedInitiator.calls = []
    _CapturedInitiator.result = CallInitiationResult(
        successful=True, call_id="CA-demo", provider="twilio"
    )


def test_demo_call_places_capped_uk_call(monkeypatch) -> None:
    client = _client(monkeypatch)
    monkeypatch.setattr(demo, "TwilioCallInitiator", _CapturedInitiator)
    response = client.post(
        "/api/v1/demo/call", json={**_VALID, "phone_number": "07700 900123"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "calling", "max_demo_seconds": 60}
    (call,) = _CapturedInitiator.calls
    assert call["target_number"] == "+447700900123"
    context = call["context"]
    assert context["source"] == "clinic_recall_demo"
    assert context["scenario"] == "demo"
    assert context["max_call_seconds"] == 60
    assert context["time_limit_seconds"] == 75
    assert context["record_call"] is False
    verify_demo_token(context["demo_token"], expected_kind="phone", secret=SECRET)


def test_demo_call_rejects_non_uk_number_before_twilio(monkeypatch) -> None:
    client = _client(monkeypatch)
    monkeypatch.setattr(demo, "TwilioCallInitiator", _CapturedInitiator)
    response = client.post(
        "/api/v1/demo/call", json={**_VALID, "phone_number": "+15551234567"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_uk_phone"
    assert _CapturedInitiator.calls == []


def test_demo_call_disabled_flag_blocks(monkeypatch) -> None:
    client = _client(monkeypatch, phone=False)
    response = client.post(
        "/api/v1/demo/call", json={**_VALID, "phone_number": "07700 900123"}
    )
    assert response.status_code == 403


def test_demo_call_repeat_phone_same_day_blocked(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_RATE_PER_PHONE_PER_DAY", "1")
    client = _client(monkeypatch)
    monkeypatch.setattr(demo, "TwilioCallInitiator", _CapturedInitiator)
    first = client.post("/api/v1/demo/call", json={**_VALID, "phone_number": "07700 900123"})
    assert first.status_code == 200
    second = client.post("/api/v1/demo/call", json={**_VALID, "phone_number": "07700 900123"})
    assert second.status_code == 429
    assert second.json()["detail"] == "phone_daily_limit"
    assert len(_CapturedInitiator.calls) == 1


def test_demo_call_surfaces_provider_failure(monkeypatch) -> None:
    client = _client(monkeypatch)
    monkeypatch.setattr(demo, "TwilioCallInitiator", _CapturedInitiator)
    _CapturedInitiator.result = CallInitiationResult(
        successful=False, provider="twilio", error="twilio_not_configured:TWILIO_ACCOUNT_SID"
    )
    response = client.post(
        "/api/v1/demo/call", json={**_VALID, "phone_number": "07700 900123"}
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "demo_call_failed"


# ---------------------------------------------------------------------------
# Capabilities contract + experience kill switch
# ---------------------------------------------------------------------------


def test_capabilities_fails_closed_without_site_key(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get("/api/v1/demo/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "experience": "off",
        "browser_enabled": False,
        "phone_enabled": False,
        "max_demo_seconds": 60,
        "turnstile_site_key": "",
    }


def test_capabilities_reports_runtime_flags_and_public_site_key(monkeypatch) -> None:
    client = _client(monkeypatch, phone=False)
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "public-site-key")
    response = client.get("/api/v1/demo/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "experience": "legacy",
        "browser_enabled": True,
        "phone_enabled": False,
        "max_demo_seconds": 60,
        "turnstile_site_key": "public-site-key",
    }


def test_experience_off_blocks_capabilities_session_call_and_ws_token(monkeypatch) -> None:
    client = _client(monkeypatch)
    monkeypatch.setattr(demo, "TwilioCallInitiator", _CapturedInitiator)
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "public-site-key")
    monkeypatch.setenv("DEMO_EXPERIENCE", "off")

    capabilities = client.get("/api/v1/demo/capabilities").json()
    assert capabilities["experience"] == "off"
    assert capabilities["browser_enabled"] is False
    assert capabilities["phone_enabled"] is False
    assert capabilities["turnstile_site_key"] == ""

    session = client.post("/api/v1/demo/session", json=_VALID)
    assert session.status_code == 403
    assert session.json()["detail"] == "demo_disabled"

    call = client.post("/api/v1/demo/call", json={**_VALID, "phone_number": "07700 900123"})
    assert call.status_code == 403
    assert call.json()["detail"] == "demo_disabled"
    assert _CapturedInitiator.calls == []

    token = issue_demo_token(
        email="owner@clinic.example.com", clinic_name="Riverside", kind="browser", secret=SECRET
    )
    with pytest.raises(DemoGateError) as exc:
        demo.validate_browser_demo_token(token)
    assert exc.value.reason == "demo_disabled"
    assert exc.value.status_code == 403


def test_unknown_experience_value_fails_closed(monkeypatch) -> None:
    client = _client(monkeypatch)
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "public-site-key")
    monkeypatch.setenv("DEMO_EXPERIENCE", "beta")
    assert client.get("/api/v1/demo/capabilities").json()["experience"] == "off"
    response = client.post("/api/v1/demo/session", json=_VALID)
    assert response.status_code == 403
    assert response.json()["detail"] == "demo_disabled"
