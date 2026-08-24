"""Unit tests for the deterministic public-demo gates (Phase B/C)."""

from __future__ import annotations

import httpx
import pytest
from src.clinic_recall.demo_gate import (
    DemoGateError,
    DemoRateLimits,
    demo_experience,
    enforce_demo_rate_limits,
    issue_demo_token,
    turnstile_site_key,
    validate_clinic_name,
    validate_uk_phone,
    validate_work_email,
    verify_demo_token,
    verify_turnstile,
)

SECRET = "unit-test-secret"


# ---------------------------------------------------------------------------
# Experience + public site key runtime configuration
# ---------------------------------------------------------------------------


def test_demo_experience_defaults_to_legacy(monkeypatch) -> None:
    monkeypatch.delenv("DEMO_EXPERIENCE", raising=False)
    assert demo_experience() == "legacy"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("off", "off"),
        ("legacy", "legacy"),
        ("parity", "parity"),
        ("  PARITY  ", "parity"),
        ("beta", "off"),
        ("", "off"),
    ],
)
def test_demo_experience_parses_and_fails_closed(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv("DEMO_EXPERIENCE", raw)
    assert demo_experience() == expected


def test_turnstile_site_key_reads_public_value(monkeypatch) -> None:
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)
    assert turnstile_site_key() == ""
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "  0xPUBLIC  ")
    assert turnstile_site_key() == "0xPUBLIC"


# ---------------------------------------------------------------------------
# Email / clinic name validation
# ---------------------------------------------------------------------------


def test_validate_work_email_accepts_and_normalises() -> None:
    assert validate_work_email("  Owner@Clinic.Example.COM ") == "owner@clinic.example.com"


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", "a" * 250 + "@x.com", "x@.com"])
def test_validate_work_email_rejects_invalid(email: str) -> None:
    with pytest.raises(DemoGateError) as exc:
        validate_work_email(email)
    assert exc.value.reason == "invalid_email"


def test_validate_clinic_name_bounds() -> None:
    assert validate_clinic_name("  Riverside Physio  ") == "Riverside Physio"
    for bad in ["", "x", "y" * 121]:
        with pytest.raises(DemoGateError):
            validate_clinic_name(bad)


# ---------------------------------------------------------------------------
# UK phone validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+447700900123", "+447700900123"),
        ("07700 900123", "+447700900123"),
        ("0044 7700 900123", "+447700900123"),
        ("+44 (20) 7946-0958", "+442079460958"),
        ("01223 456789", "+441223456789"),
    ],
)
def test_validate_uk_phone_accepts_uk_numbers(raw: str, expected: str) -> None:
    assert validate_uk_phone(raw) == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("+15551234567", "invalid_uk_phone"),  # US
        ("+33123456789", "invalid_uk_phone"),  # France
        ("+44", "invalid_uk_phone"),
        ("not a phone", "invalid_uk_phone"),
        ("+449098790000", "blocked_phone_range"),  # premium rate
        ("+448081570000", "blocked_phone_range"),  # freephone/special
        ("+447037900000", "blocked_phone_range"),  # personal numbering
        ("+447640900000", "blocked_phone_range"),  # pager
    ],
)
def test_validate_uk_phone_rejects_non_uk_and_premium(raw: str, reason: str) -> None:
    with pytest.raises(DemoGateError) as exc:
        validate_uk_phone(raw)
    assert exc.value.reason == reason


# ---------------------------------------------------------------------------
# Demo tokens
# ---------------------------------------------------------------------------


def test_demo_token_roundtrip() -> None:
    token = issue_demo_token(
        email="owner@clinic.example.com",
        clinic_name="Riverside Physio",
        kind="browser",
        secret=SECRET,
        now=1_000_000.0,
    )
    payload = verify_demo_token(token, expected_kind="browser", secret=SECRET, now=1_000_100.0)
    assert payload["kind"] == "browser"
    assert payload["clinic_name"] == "Riverside Physio"
    assert "owner@" not in token  # raw PII never embedded


def test_demo_token_expires() -> None:
    token = issue_demo_token(
        email="a@b.co", clinic_name="Clinic", kind="phone", secret=SECRET, now=1_000_000.0
    )
    with pytest.raises(DemoGateError) as exc:
        verify_demo_token(token, expected_kind="phone", secret=SECRET, now=1_000_301.0)
    assert exc.value.reason == "demo_token_expired"


def test_demo_token_rejects_wrong_kind_and_forgery() -> None:
    token = issue_demo_token(
        email="a@b.co", clinic_name="Clinic", kind="browser", secret=SECRET, now=1_000_000.0
    )
    with pytest.raises(DemoGateError):
        verify_demo_token(token, expected_kind="phone", secret=SECRET, now=1_000_010.0)
    with pytest.raises(DemoGateError):
        verify_demo_token(token, expected_kind="browser", secret="other-secret", now=1_000_010.0)
    payload_hex, signature = token.split(".", 1)
    tampered = bytes.fromhex(payload_hex).replace(b"browser", b"phonexx").hex()
    with pytest.raises(DemoGateError):
        verify_demo_token(f"{tampered}.{signature}", expected_kind="phone", secret=SECRET, now=1_000_010.0)
    with pytest.raises(DemoGateError):
        verify_demo_token("garbage", expected_kind="browser", secret=SECRET)


def test_demo_token_fails_closed_without_secret(monkeypatch) -> None:
    monkeypatch.delenv("DEMO_TOKEN_SECRET", raising=False)
    with pytest.raises(DemoGateError) as exc:
        issue_demo_token(email="a@b.co", clinic_name="Clinic", kind="browser")
    assert exc.value.status_code == 503
    with pytest.raises(DemoGateError):
        verify_demo_token("anything", expected_kind="browser")


# ---------------------------------------------------------------------------
# Turnstile
# ---------------------------------------------------------------------------


def _turnstile_transport(success: bool) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        # Pin the exact siteverify endpoint (live smoke 2026-07-08 caught a
        # wrong-path bug that a host-only assertion let through).
        assert str(request.url) == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
        return httpx.Response(200, json={"success": success})

    return httpx.MockTransport(handler)


def test_verify_turnstile_success() -> None:
    verify_turnstile("tok", "203.0.113.9", secret="ts-secret", transport=_turnstile_transport(True))


def test_verify_turnstile_failure_and_fail_closed() -> None:
    with pytest.raises(DemoGateError) as exc:
        verify_turnstile("tok", None, secret="ts-secret", transport=_turnstile_transport(False))
    assert exc.value.reason == "captcha_failed"
    with pytest.raises(DemoGateError) as missing:
        verify_turnstile("tok", None, secret="")
    assert missing.value.status_code == 503
    with pytest.raises(DemoGateError) as empty:
        verify_turnstile("", None, secret="ts-secret")
    assert empty.value.reason == "captcha_required"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(DemoGateError) as outage:
        verify_turnstile("tok", None, secret="ts-secret", transport=httpx.MockTransport(boom))
    assert outage.value.status_code == 503


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


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


LIMITS = DemoRateLimits(per_ip_per_hour=2, per_phone_per_day=1, global_per_day=3)


def test_rate_limit_blocks_ip_after_threshold() -> None:
    manager = _FakeRedisManager()
    enforce_demo_rate_limits(manager, remote_ip="203.0.113.9", limits=LIMITS)
    enforce_demo_rate_limits(manager, remote_ip="203.0.113.9", limits=LIMITS)
    with pytest.raises(DemoGateError) as exc:
        enforce_demo_rate_limits(manager, remote_ip="203.0.113.9", limits=LIMITS)
    assert exc.value.reason == "too_many_requests"
    assert exc.value.status_code == 429


def test_rate_limit_blocks_phone_daily_repeat() -> None:
    manager = _FakeRedisManager()
    enforce_demo_rate_limits(manager, remote_ip="198.51.100.1", phone="+447700900123", limits=LIMITS)
    with pytest.raises(DemoGateError) as exc:
        enforce_demo_rate_limits(manager, remote_ip="198.51.100.2", phone="+447700900123", limits=LIMITS)
    assert exc.value.reason == "phone_daily_limit"


def test_rate_limit_blocks_global_budget() -> None:
    manager = _FakeRedisManager()
    for ip in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
        enforce_demo_rate_limits(manager, remote_ip=ip, limits=LIMITS)
    with pytest.raises(DemoGateError) as exc:
        enforce_demo_rate_limits(manager, remote_ip="198.51.100.4", limits=LIMITS)
    assert exc.value.reason == "demo_capacity_reached"


def test_rate_limit_fails_closed_without_redis() -> None:
    with pytest.raises(DemoGateError) as exc:
        enforce_demo_rate_limits(None, remote_ip="203.0.113.9", limits=LIMITS)
    assert exc.value.status_code == 503

    class _BrokenClient:
        def pipeline(self):
            raise ConnectionError("redis down")

    class _BrokenManager:
        redis_client = _BrokenClient()

    with pytest.raises(DemoGateError) as broken:
        enforce_demo_rate_limits(_BrokenManager(), remote_ip="203.0.113.9", limits=LIMITS)
    assert broken.value.reason == "rate_limiter_unavailable"
