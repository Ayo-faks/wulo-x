"""Tests for demo-mode trusted stream parameters on the Twilio TwiML endpoint."""

from __future__ import annotations

from apps.artagent.backend.api.v1.endpoints.voice import router
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.clinic_recall.demo_gate import issue_demo_token

SECRET = "twiml-demo-secret"


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/voice")
    return app


def _base_env(monkeypatch) -> None:
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setenv("TWILIO_MEDIA_STREAM_URL", "wss://clinic.example.test/api/v1/twilio/stream")
    monkeypatch.setenv("DEMO_TOKEN_SECRET", SECRET)
    # Demo TwiML now honours the runtime kill switches; a valid token alone is
    # no longer sufficient once the demo is disabled.
    monkeypatch.setenv("DEMO_PHONE_ENABLED", "true")
    monkeypatch.delenv("DEMO_EXPERIENCE", raising=False)


def test_demo_twiml_with_valid_token_streams_demo_scenario(monkeypatch) -> None:
    _base_env(monkeypatch)
    token = issue_demo_token(
        email="owner@clinic.example.com", clinic_name="Riverside", kind="phone", secret=SECRET
    )
    client = TestClient(_app())

    response = client.get(
        "/api/v1/voice/twilio/twiml",
        params={
            "session_id": "twilio-demo-1",
            "source": "clinic_recall_demo",
            "scenario": "demo",
            "max_call_seconds": "60",
            "demo_token": token,
            # Hostile extras must never reach the stream:
            "clinic_id": "clinic-a",
            "patient_id": "patient-a",
            "record_call": "true",
        },
    )

    assert response.status_code == 200
    assert '<Parameter name="scenario" value="demo" />' in response.text
    assert '<Parameter name="max_call_seconds" value="60" />' in response.text
    assert '<Parameter name="source" value="clinic_recall_demo" />' in response.text
    assert "clinic_id" not in response.text
    assert "patient_id" not in response.text
    assert "record_call" not in response.text
    assert "demo_token" not in response.text


def test_demo_twiml_fails_closed_without_token(monkeypatch) -> None:
    _base_env(monkeypatch)
    client = TestClient(_app())

    response = client.get(
        "/api/v1/voice/twilio/twiml",
        params={"source": "clinic_recall_demo", "scenario": "demo"},
    )

    assert response.status_code == 200
    assert "<Hangup />" in response.text
    assert "<Connect>" not in response.text


def test_demo_twiml_fails_closed_with_browser_token(monkeypatch) -> None:
    """A browser-kind token must not authorise a phone call."""
    _base_env(monkeypatch)
    token = issue_demo_token(
        email="owner@clinic.example.com", clinic_name="Riverside", kind="browser", secret=SECRET
    )
    client = TestClient(_app())

    response = client.get(
        "/api/v1/voice/twilio/twiml",
        params={"source": "clinic_recall_demo", "demo_token": token},
    )

    assert "<Hangup />" in response.text
    assert "<Connect>" not in response.text


def test_demo_twiml_fails_closed_with_forged_token(monkeypatch) -> None:
    _base_env(monkeypatch)
    token = issue_demo_token(
        email="owner@clinic.example.com", clinic_name="Riverside", kind="phone", secret="wrong-secret"
    )
    client = TestClient(_app())

    response = client.get(
        "/api/v1/voice/twilio/twiml",
        params={"source": "clinic_recall_demo", "demo_token": token},
    )

    assert "<Hangup />" in response.text
    assert "<Connect>" not in response.text


def test_demo_twiml_fails_closed_when_experience_off(monkeypatch) -> None:
    """A still-valid token must not open a media stream after the kill switch."""
    _base_env(monkeypatch)
    monkeypatch.setenv("DEMO_EXPERIENCE", "off")
    token = issue_demo_token(
        email="owner@clinic.example.com", clinic_name="Riverside", kind="phone", secret=SECRET
    )
    client = TestClient(_app())

    response = client.get(
        "/api/v1/voice/twilio/twiml",
        params={"source": "clinic_recall_demo", "demo_token": token},
    )

    assert "<Hangup />" in response.text
    assert "<Connect>" not in response.text


def test_demo_twiml_fails_closed_when_phone_flag_disabled(monkeypatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("DEMO_PHONE_ENABLED", "false")
    token = issue_demo_token(
        email="owner@clinic.example.com", clinic_name="Riverside", kind="phone", secret=SECRET
    )
    client = TestClient(_app())

    response = client.get(
        "/api/v1/voice/twilio/twiml",
        params={"source": "clinic_recall_demo", "demo_token": token},
    )

    assert "<Hangup />" in response.text
    assert "<Connect>" not in response.text
