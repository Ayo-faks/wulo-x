"""Deterministic gating for the public 60-second demo call experience.

Everything in this module is deterministic and fails closed:
- signed short-lived demo tokens (HMAC, no external deps)
- Turnstile server-side verification
- Redis-backed rate limits (per IP, per phone, global daily budget)
- UK-only E.164 phone validation
- work-email shape validation

The AI model never participates in any of these decisions (AGENTS.md §2).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass

import httpx

_TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_UK_E164_RE = re.compile(r"^\+44[1-9]\d{8,9}$")

# UK ranges we will dial for demos: geographic (+441/+442), non-geographic
# standard rate (+443), and mobiles (+447) excluding personal-numbering
# (+4470) and pager (+4476) ranges. Everything else (premium, freephone,
# corporate) is refused.
_UK_ALLOWED_PREFIXES = ("+441", "+442", "+443", "+447")
_UK_BLOCKED_PREFIXES = ("+4470", "+4476")

DEMO_TOKEN_TTL_SECONDS = 300
DEMO_SCENARIO = "demo"


class DemoGateError(Exception):
    """Raised when a deterministic demo gate rejects a request."""

    def __init__(self, reason: str, status_code: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def demo_secret() -> str:
    """Secret used to sign demo tokens. Empty secret disables demo issuance."""
    return os.getenv("DEMO_TOKEN_SECRET", "")


def browser_demo_enabled() -> bool:
    return os.getenv("DEMO_BROWSER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def phone_demo_enabled() -> bool:
    return os.getenv("DEMO_PHONE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def demo_max_seconds() -> int:
    return _env_int("DEMO_MAX_SECONDS", 60)


# Known public demo experiences. "legacy" is the existing no-tool persona;
# "parity" is reserved for the journey-first experience; "off" hides the demo.
DEMO_EXPERIENCES = ("off", "legacy", "parity")


def demo_experience() -> str:
    """Active public demo experience; unknown values fail closed to "off".

    The default is "legacy" so environments that only set the existing
    DEMO_*_ENABLED flags keep their current behaviour unchanged.
    """
    raw = os.getenv("DEMO_EXPERIENCE", "legacy").strip().lower()
    return raw if raw in DEMO_EXPERIENCES else "off"


def turnstile_site_key() -> str:
    """Public Cloudflare Turnstile site key (safe to expose to browsers)."""
    return os.getenv("TURNSTILE_SITE_KEY", "").strip()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def validate_work_email(email: str) -> str:
    candidate = (email or "").strip().lower()
    if not candidate or len(candidate) > 254 or not _EMAIL_RE.match(candidate):
        raise DemoGateError("invalid_email")
    return candidate


def validate_clinic_name(clinic_name: str) -> str:
    candidate = (clinic_name or "").strip()
    if not (2 <= len(candidate) <= 120):
        raise DemoGateError("invalid_clinic_name")
    return candidate


def validate_uk_phone(phone: str) -> str:
    """Accept only UK E.164 mobile/landline numbers; refuse premium-rate ranges."""
    candidate = re.sub(r"[\s()-]", "", phone or "")
    if candidate.startswith("0044"):
        candidate = f"+44{candidate[4:]}"
    if candidate.startswith("07") or candidate.startswith("01") or candidate.startswith("02"):
        candidate = f"+44{candidate[1:]}"
    if not _UK_E164_RE.match(candidate):
        raise DemoGateError("invalid_uk_phone")
    if not candidate.startswith(_UK_ALLOWED_PREFIXES) or candidate.startswith(_UK_BLOCKED_PREFIXES):
        raise DemoGateError("blocked_phone_range")
    return candidate


# ---------------------------------------------------------------------------
# Turnstile
# ---------------------------------------------------------------------------


def verify_turnstile(
    token: str,
    remote_ip: str | None,
    *,
    secret: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Server-side Cloudflare Turnstile verification. Fails closed."""
    secret = secret if secret is not None else os.getenv("TURNSTILE_SECRET_KEY", "")
    if not secret:
        raise DemoGateError("turnstile_not_configured", status_code=503)
    if not token:
        raise DemoGateError("captcha_required")
    data = {"secret": secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        with httpx.Client(timeout=10.0, transport=transport) as client:
            response = client.post(_TURNSTILE_VERIFY_URL, data=data)
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DemoGateError("captcha_unavailable", status_code=503) from exc
    if payload.get("success") is not True:
        raise DemoGateError("captcha_failed", status_code=403)


# ---------------------------------------------------------------------------
# Rate limiting (Redis; fails closed when Redis is unavailable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoRateLimits:
    per_ip_per_hour: int
    per_phone_per_day: int
    global_per_day: int

    @classmethod
    def from_env(cls) -> DemoRateLimits:
        return cls(
            per_ip_per_hour=_env_int("DEMO_RATE_PER_IP_PER_HOUR", 3),
            per_phone_per_day=_env_int("DEMO_RATE_PER_PHONE_PER_DAY", 1),
            global_per_day=_env_int("DEMO_RATE_GLOBAL_PER_DAY", 100),
        )


def _bump(redis_client, key: str, ttl_seconds: int) -> int:
    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.expire(key, ttl_seconds, nx=True)
    count, _ = pipe.execute()
    return int(count)


def _hashed(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def enforce_demo_rate_limits(
    redis_manager,
    *,
    remote_ip: str | None,
    phone: str | None = None,
    limits: DemoRateLimits | None = None,
) -> None:
    """Deterministic abuse gate. Raises DemoGateError on any failure or breach."""
    limits = limits or DemoRateLimits.from_env()
    client = getattr(redis_manager, "redis_client", None)
    if client is None:
        raise DemoGateError("rate_limiter_unavailable", status_code=503)
    try:
        day_stamp = time.strftime("%Y%m%d", time.gmtime())
        if _bump(client, f"demo:rate:global:{day_stamp}", 86400) > limits.global_per_day:
            raise DemoGateError("demo_capacity_reached", status_code=429)
        if remote_ip and _bump(client, f"demo:rate:ip:{_hashed(remote_ip)}", 3600) > limits.per_ip_per_hour:
            raise DemoGateError("too_many_requests", status_code=429)
        if phone and _bump(client, f"demo:rate:phone:{_hashed(phone)}:{day_stamp}", 86400) > limits.per_phone_per_day:
            raise DemoGateError("phone_daily_limit", status_code=429)
    except DemoGateError:
        raise
    except Exception as exc:  # Redis outage → fail closed
        raise DemoGateError("rate_limiter_unavailable", status_code=503) from exc


# ---------------------------------------------------------------------------
# Signed demo tokens
# ---------------------------------------------------------------------------


def _sign(payload_b: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_b, hashlib.sha256).hexdigest()


def issue_demo_token(
    *,
    email: str,
    clinic_name: str,
    kind: str,
    secret: str | None = None,
    now: float | None = None,
    ttl_seconds: int = DEMO_TOKEN_TTL_SECONDS,
) -> str:
    """Issue a short-lived signed token proving the demo gate was passed."""
    secret = secret if secret is not None else demo_secret()
    if not secret:
        raise DemoGateError("demo_not_configured", status_code=503)
    if kind not in {"browser", "phone"}:
        raise DemoGateError("invalid_token_kind")
    issued_at = int(now if now is not None else time.time())
    payload = {
        "sid": f"demo-{uuid.uuid4().hex}",
        "kind": kind,
        "email_hash": _hashed(email),
        "clinic_name": clinic_name[:64],
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    payload_b = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = _sign(payload_b, secret)
    return f"{payload_b.hex()}.{signature}"


def verify_demo_token(
    token: str,
    *,
    expected_kind: str,
    secret: str | None = None,
    now: float | None = None,
) -> dict:
    """Verify a demo token. Raises DemoGateError on any mismatch. Fails closed."""
    secret = secret if secret is not None else demo_secret()
    if not secret:
        raise DemoGateError("demo_not_configured", status_code=503)
    try:
        payload_hex, signature = (token or "").split(".", 1)
        payload_b = bytes.fromhex(payload_hex)
    except ValueError:
        raise DemoGateError("invalid_demo_token", status_code=401) from None
    if not hmac.compare_digest(_sign(payload_b, secret), signature):
        raise DemoGateError("invalid_demo_token", status_code=401)
    try:
        payload = json.loads(payload_b)
    except ValueError:
        raise DemoGateError("invalid_demo_token", status_code=401) from None
    if payload.get("kind") != expected_kind:
        raise DemoGateError("invalid_demo_token", status_code=401)
    current = now if now is not None else time.time()
    if current >= float(payload.get("exp", 0)):
        raise DemoGateError("demo_token_expired", status_code=401)
    return payload
