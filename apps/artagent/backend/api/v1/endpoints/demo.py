"""Public demo endpoints for the Motics-style 60-second Clinic Recall demo.

Both endpoints are unauthenticated but deterministically gated:
Turnstile → rate limits → validation → short-lived signed token.
The AI model plays no part in any gating decision (AGENTS.md §2).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from src.clinic_recall.demo_gate import (
    DemoGateError,
    browser_demo_enabled,
    demo_experience,
    demo_max_seconds,
    enforce_demo_rate_limits,
    issue_demo_token,
    phone_demo_enabled,
    turnstile_site_key,
    validate_clinic_name,
    validate_uk_phone,
    validate_work_email,
    verify_demo_token,
    verify_turnstile,
)
from src.clinic_recall.voice_worker import TwilioCallInitiator
from utils.ml_logging import get_logger

logger = get_logger("api.v1.demo")
router = APIRouter()

_DEMO_SOURCE = "clinic_recall_demo"
_DEMO_SCENARIO = "demo"
# Twilio-side hard cap: demo length plus headroom for the wrap-up line.
_TIME_LIMIT_HEADROOM_S = 15


class DemoSessionRequest(BaseModel):
    work_email: str = Field(..., max_length=254)
    clinic_name: str = Field(..., max_length=120)
    turnstile_token: str = Field(..., max_length=4096)


class DemoSessionResponse(BaseModel):
    demo_token: str
    expires_in_seconds: int
    max_demo_seconds: int


class DemoCallRequest(DemoSessionRequest):
    phone_number: str = Field(..., max_length=32)


class DemoCallResponse(BaseModel):
    status: str
    max_demo_seconds: int


class DemoCapabilitiesResponse(BaseModel):
    experience: str
    browser_enabled: bool
    phone_enabled: bool
    max_demo_seconds: int
    turnstile_site_key: str


@router.get("/capabilities", response_model=DemoCapabilitiesResponse)
def get_demo_capabilities() -> DemoCapabilitiesResponse:
    """Public, non-sensitive availability contract for the landing demo widget.

    Deterministic and fail-closed: with the experience off or no public
    Turnstile site key configured the demo reports itself off and exposes
    nothing actionable. The site key is public by design; secrets never
    appear in this response.
    """
    experience = demo_experience()
    site_key = turnstile_site_key()
    active = experience != "off" and bool(site_key)
    return DemoCapabilitiesResponse(
        experience=experience if active else "off",
        browser_enabled=bool(active and browser_demo_enabled()),
        phone_enabled=bool(active and phone_demo_enabled()),
        max_demo_seconds=demo_max_seconds(),
        turnstile_site_key=site_key if active else "",
    )


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _gate(request: Request, payload: DemoSessionRequest, *, phone: str | None = None) -> tuple[str, str]:
    """Run every deterministic demo gate; raises HTTPException on rejection."""
    try:
        email = validate_work_email(payload.work_email)
        clinic_name = validate_clinic_name(payload.clinic_name)
        remote_ip = _client_ip(request)
        verify_turnstile(payload.turnstile_token, remote_ip)
        enforce_demo_rate_limits(
            getattr(request.app.state, "redis", None),
            remote_ip=remote_ip,
            phone=phone,
        )
    except DemoGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc
    return email, clinic_name


@router.post("/session", response_model=DemoSessionResponse)
def create_demo_session(payload: DemoSessionRequest, request: Request) -> DemoSessionResponse:
    """Issue a short-lived token for the browser mic demo widget."""
    if demo_experience() == "off" or not browser_demo_enabled():
        raise HTTPException(status_code=403, detail="demo_disabled")
    email, clinic_name = _gate(request, payload)
    try:
        token = issue_demo_token(email=email, clinic_name=clinic_name, kind="browser")
    except DemoGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc
    logger.info("Browser demo session issued | clinic_name=%s", clinic_name[:32])
    return DemoSessionResponse(
        demo_token=token,
        expires_in_seconds=300,
        max_demo_seconds=demo_max_seconds(),
    )


@router.post("/call", response_model=DemoCallResponse)
def create_demo_call(payload: DemoCallRequest, request: Request) -> DemoCallResponse:
    """Place a real outbound demo phone call, capped at the demo duration."""
    if demo_experience() == "off" or not phone_demo_enabled():
        raise HTTPException(status_code=403, detail="demo_disabled")
    try:
        phone = validate_uk_phone(payload.phone_number)
    except DemoGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc
    email, clinic_name = _gate(request, payload, phone=phone)
    try:
        token = issue_demo_token(email=email, clinic_name=clinic_name, kind="phone")
    except DemoGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc

    max_seconds = demo_max_seconds()
    initiator = TwilioCallInitiator()
    result = initiator.initiate_call(
        target_number=phone,
        context={
            "source": _DEMO_SOURCE,
            "scenario": _DEMO_SCENARIO,
            "demo_token": token,
            "max_call_seconds": max_seconds,
            "time_limit_seconds": max_seconds + _TIME_LIMIT_HEADROOM_S,
            # Demo calls are never recorded.
            "record_call": False,
        },
    )
    if not result.successful:
        logger.warning("Demo call initiation failed | error=%s", result.error)
        raise HTTPException(status_code=502, detail="demo_call_failed")
    logger.info(
        "Demo call initiated | call_id=%s clinic_name=%s",
        result.call_id,
        clinic_name[:32],
    )
    return DemoCallResponse(status="calling", max_demo_seconds=max_seconds)


def validate_browser_demo_token(token: str) -> dict:
    """Verify a browser demo token for the websocket upgrade path.

    Raises DemoGateError (fails closed) on any problem, including the
    runtime experience kill switch being off.
    """
    if demo_experience() == "off" or not browser_demo_enabled():
        raise DemoGateError("demo_disabled", status_code=403)
    return verify_demo_token(token, expected_kind="browser")
