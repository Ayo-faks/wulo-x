"""Deterministic Phase 3 voice hand-off worker for Clinic Recall."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.sax.saxutils import escape as xml_escape

import httpx

from . import voice_planner as _voice_planner
from .durable.callbacks import EffectTokenError, parse_effect_token

VoiceCadenceResult = _voice_planner.VoiceCadenceResult
run_voice_cadence = _voice_planner.run_voice_cadence

# Deterministic disclosure played by the provider (never by the model) on every
# call where recording consent was granted.
RECORDING_ANNOUNCEMENT = "This call may be recorded for quality and safety."


def _twilio_recording_status_callback_url(effect_token: str | None = None) -> str:
    explicit = os.getenv("TWILIO_RECORDING_STATUS_CALLBACK_URL", "")
    if explicit:
        return _with_effect_token(explicit, effect_token)
    base_url = os.getenv("TWILIO_WEBHOOK_BASE_URL") or os.getenv("BASE_URL") or ""
    if not base_url:
        return ""
    return _with_effect_token(
        f"{base_url.rstrip('/')}/api/v1/voice/twilio/recording-status",
        effect_token,
    )


def _twilio_voice_status_callback_url(effect_token: str | None = None) -> str:
    explicit = os.getenv("TWILIO_VOICE_STATUS_CALLBACK_URL", "")
    if explicit:
        return _with_effect_token(explicit, effect_token)
    base_url = os.getenv("TWILIO_WEBHOOK_BASE_URL") or os.getenv("BASE_URL") or ""
    if not base_url:
        return ""
    return _with_effect_token(
        f"{base_url.rstrip('/')}/api/v1/voice/twilio/call-status",
        effect_token,
    )


class CallInitiationDisposition(StrEnum):
    """Closed provider-create outcomes used by durable CALL dispatch."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"
    NOT_DISPATCHED = "not_dispatched"


class CallInitiationReason(StrEnum):
    """Allowlisted non-sensitive reasons for CALL create outcomes."""

    PROVIDER_ACCEPTED = "provider_accepted"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_VALIDATION_REJECTED = "provider_validation_rejected"
    PROVIDER_AUTH_REJECTED = "provider_auth_rejected"
    PROVIDER_SERVER_ERROR = "provider_server_error"
    TRANSPORT_ERROR = "transport_error"
    MISSING_CALL_SID = "missing_call_sid"
    MALFORMED_RESPONSE = "malformed_response"
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_EFFECT_TOKEN = "invalid_effect_token"
    DURABLE_POLICY_REJECTED = "durable_policy_rejected"


@dataclass(frozen=True)
class CallInitiationResult:
    """Provider-agnostic result for one outbound call request."""

    successful: bool
    call_id: str | None = None
    provider: str = "art"
    error: str | None = None
    disposition: CallInitiationDisposition | None = None
    reason_code: CallInitiationReason | None = None


@runtime_checkable
class CallInitiator(Protocol):
    """Synchronous outbound-call interface used by the deterministic worker."""

    name: str

    def initiate_call(self, *, target_number: str, context: dict[str, Any]) -> CallInitiationResult:
        """Place one outbound call through ART/ACS or a fake provider."""
        ...


class ArtCallInitiator:
    """HTTP adapter over ART's native POST /api/v1/calls/initiate endpoint."""

    name = "art_http"

    def __init__(self, endpoint: str | None = None, timeout_seconds: float = 10.0) -> None:
        self.endpoint = endpoint or _default_call_endpoint()
        self.timeout_seconds = timeout_seconds

    def initiate_call(self, *, target_number: str, context: dict[str, Any]) -> CallInitiationResult:
        response = httpx.post(
            self.endpoint,
            json={"target_number": target_number, "context": context},
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            return CallInitiationResult(
                successful=False,
                provider=self.name,
                error=f"http_{response.status_code}",
            )
        payload = response.json()
        return CallInitiationResult(
            successful=True,
            call_id=payload.get("call_id") or payload.get("callId"),
            provider=self.name,
        )


class TwilioCallInitiator:
    """Twilio Programmable Voice adapter for the Recall media-stream fallback."""

    name = "twilio"

    def __init__(
        self,
        *,
        account_sid: str | None = None,
        auth_token: str | None = None,
        from_number: str | None = None,
        twiml_url: str | None = None,
        media_stream_url: str | None = None,
        status_callback_url: str | None = None,
        inline_twiml: bool | None = None,
        api_base_url: str | None = None,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.account_sid: str = account_sid or os.getenv("TWILIO_ACCOUNT_SID") or ""
        self.auth_token: str = auth_token or os.getenv("TWILIO_AUTH_TOKEN") or ""
        self.from_number: str = from_number or _twilio_voice_from_number()
        self.twiml_url: str = twiml_url or _default_twilio_twiml_url()
        self.media_stream_url: str = (
            media_stream_url or _default_twilio_media_stream_url()
        )
        self.status_callback_url: str = (
            status_callback_url or _twilio_voice_status_callback_url()
        )
        self.inline_twiml = _env_bool("TWILIO_VOICE_INLINE_TWIML", False) if inline_twiml is None else inline_twiml
        self.api_base_url: str = (
            api_base_url or os.getenv("TWILIO_API_BASE_URL") or "https://api.twilio.com"
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def initiate_call(self, *, target_number: str, context: dict[str, Any]) -> CallInitiationResult:
        missing = self._missing_configuration()
        if missing:
            return CallInitiationResult(
                successful=False,
                provider=self.name,
                error=f"twilio_not_configured:{','.join(missing)}",
                disposition=CallInitiationDisposition.NOT_DISPATCHED,
                reason_code=CallInitiationReason.INVALID_CONFIGURATION,
            )

        effect_token = str(context.get("effect_token") or "").strip() or None
        recording_effect_token = (
            str(context.get("recording_effect_token") or "").strip() or None
        )
        for callback_token in (effect_token, recording_effect_token):
            if callback_token is None:
                continue
            try:
                parse_effect_token(callback_token)
            except EffectTokenError:
                return CallInitiationResult(
                    successful=False,
                    provider=self.name,
                    error="invalid_effect_token",
                    disposition=CallInitiationDisposition.NOT_DISPATCHED,
                    reason_code=CallInitiationReason.INVALID_EFFECT_TOKEN,
                )

        durable_recall = context.get("source") == "clinic_recall_voice_worker"
        if durable_recall and effect_token is None:
            return CallInitiationResult(
                successful=False,
                provider=self.name,
                error=CallInitiationReason.INVALID_EFFECT_TOKEN.value,
                disposition=CallInitiationDisposition.NOT_DISPATCHED,
                reason_code=CallInitiationReason.INVALID_EFFECT_TOKEN,
            )
        if durable_recall and (
            self.inline_twiml
            or context.get("record_call") is not False
            or recording_effect_token is not None
        ):
            return CallInitiationResult(
                successful=False,
                provider=self.name,
                error=CallInitiationReason.DURABLE_POLICY_REJECTED.value,
                disposition=CallInitiationDisposition.NOT_DISPATCHED,
                reason_code=CallInitiationReason.DURABLE_POLICY_REJECTED,
            )
        clinic_recall_context = (
            str(context.get("source") or "").startswith("clinic_recall")
            or str(context.get("scenario") or "").strip().lower()
            in {"rebooking", "inbound_clinic"}
        )
        recording_requested = (
            context.get("record_call") is not None
            and context.get("record_call") is not False
        )
        if clinic_recall_context and not durable_recall and (
            recording_requested or recording_effect_token is not None
        ):
            return CallInitiationResult(
                successful=False,
                provider=self.name,
                error=CallInitiationReason.DURABLE_POLICY_REJECTED.value,
                disposition=CallInitiationDisposition.NOT_DISPATCHED,
                reason_code=CallInitiationReason.DURABLE_POLICY_REJECTED,
            )
        if durable_recall and not self.status_callback_url:
            return CallInitiationResult(
                successful=False,
                provider=self.name,
                error=CallInitiationReason.INVALID_CONFIGURATION.value,
                disposition=CallInitiationDisposition.NOT_DISPATCHED,
                reason_code=CallInitiationReason.INVALID_CONFIGURATION,
            )

        session_id = f"twilio-session-{uuid.uuid4().hex}"
        stream_parameters = _twilio_stream_parameters(context, session_id)
        data: dict[str, str | list[str]] = {
            "From": self.from_number,
            "To": target_number,
        }
        if durable_recall:
            data["MachineDetection"] = "Enable"
        if self.inline_twiml:
            data["Twiml"] = _twilio_connect_stream_twiml(self.media_stream_url, stream_parameters)
        else:
            instruction_parameters = (
                {"source": "clinic_recall_voice_worker"}
                if durable_recall
                else dict(stream_parameters)
            )
            if effect_token is not None:
                instruction_parameters["effect_token"] = effect_token
            data["Url"] = _url_with_query(self.twiml_url, instruction_parameters)

        if context.get("record_call") is True:
            data["Record"] = "true"
            data["RecordingChannels"] = "dual"
            recording_callback = _twilio_recording_status_callback_url(
                recording_effect_token
            )
            if recording_callback:
                data["RecordingStatusCallback"] = recording_callback
                data["RecordingStatusCallbackEvent"] = "completed"
        time_limit_seconds = context.get("time_limit_seconds")
        if time_limit_seconds is not None:
            try:
                data["TimeLimit"] = str(max(1, int(time_limit_seconds)))
            except (TypeError, ValueError):
                pass
        if self.status_callback_url:
            data["StatusCallback"] = _with_effect_token(
                self.status_callback_url,
                effect_token,
            )
            data["StatusCallbackEvent"] = ["initiated", "ringing", "answered", "completed"]

        url = f"{self.api_base_url}/2010-04-01/Accounts/{self.account_sid}/Calls.json"
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = client.post(
                    url,
                    data=data,
                    auth=(self.account_sid, self.auth_token),
                )
        except httpx.HTTPError:
            return CallInitiationResult(
                successful=False,
                provider=self.name,
                error=CallInitiationReason.TRANSPORT_ERROR.value,
                disposition=CallInitiationDisposition.AMBIGUOUS,
                reason_code=CallInitiationReason.TRANSPORT_ERROR,
            )

        payload, parsed = _safe_json(response)
        if 400 <= response.status_code < 500:
            reason = (
                CallInitiationReason.PROVIDER_AUTH_REJECTED
                if response.status_code in {401, 403}
                else CallInitiationReason.PROVIDER_VALIDATION_REJECTED
            )
            return CallInitiationResult(
                successful=False,
                provider=self.name,
                error=reason.value,
                disposition=CallInitiationDisposition.REJECTED,
                reason_code=reason,
            )
        if response.status_code >= 500:
            return CallInitiationResult(
                successful=False,
                provider=self.name,
                error=CallInitiationReason.PROVIDER_SERVER_ERROR.value,
                disposition=CallInitiationDisposition.AMBIGUOUS,
                reason_code=CallInitiationReason.PROVIDER_SERVER_ERROR,
            )
        if not parsed:
            return CallInitiationResult(
                successful=False,
                provider=self.name,
                error=CallInitiationReason.MALFORMED_RESPONSE.value,
                disposition=CallInitiationDisposition.AMBIGUOUS,
                reason_code=CallInitiationReason.MALFORMED_RESPONSE,
            )
        call_id = payload.get("sid") or payload.get("call_sid") or payload.get("callSid")
        if not isinstance(call_id, str) or not call_id.strip():
            return CallInitiationResult(
                successful=False,
                provider=self.name,
                error=CallInitiationReason.MISSING_CALL_SID.value,
                disposition=CallInitiationDisposition.AMBIGUOUS,
                reason_code=CallInitiationReason.MISSING_CALL_SID,
            )
        return CallInitiationResult(
            successful=True,
            call_id=call_id,
            provider=self.name,
            disposition=CallInitiationDisposition.ACCEPTED,
            reason_code=CallInitiationReason.PROVIDER_ACCEPTED,
        )

    def _missing_configuration(self) -> list[str]:
        missing = []
        if not self.account_sid:
            missing.append("TWILIO_ACCOUNT_SID")
        if not self.auth_token:
            missing.append("TWILIO_AUTH_TOKEN")
        if not self.from_number:
            missing.append("TWILIO_FROM_PHONE_NUMBER")
        if self.inline_twiml and not self.media_stream_url:
            missing.append("TWILIO_MEDIA_STREAM_URL")
        if not self.inline_twiml and not self.twiml_url:
            missing.append("TWILIO_VOICE_TWIML_URL")
        return missing


class FakeCallInitiator:
    """Offline call initiator used by tests and local ART simulations."""

    name = "fake"

    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.calls: list[dict[str, Any]] = []

    def initiate_call(self, *, target_number: str, context: dict[str, Any]) -> CallInitiationResult:
        self.calls.append({"target_number": target_number, "context": context})
        call_id = f"fake-call-{len(self.calls)}"
        return CallInitiationResult(
            successful=self.success,
            call_id=call_id if self.success else None,
            provider=self.name,
            error=None if self.success else "fake_call_failure",
        )


def build_call_initiator(provider: str | None = None) -> CallInitiator:
    """Build the configured outbound voice provider."""
    selected = (provider or os.getenv("VOICE_PROVIDER") or "auto").strip().lower()
    if selected in {"acs", "art", "art_http"}:
        return ArtCallInitiator()
    if selected == "twilio":
        return TwilioCallInitiator()
    if selected == "auto":
        if _is_art_call_configured():
            return ArtCallInitiator()
        if _is_twilio_voice_configured():
            return TwilioCallInitiator()
        return ArtCallInitiator()
    raise ValueError("VOICE_PROVIDER must be one of auto, acs, art, or twilio")


def _default_call_endpoint() -> str:
    explicit = os.getenv("CLINIC_RECALL_CALL_INITIATE_URL")
    if explicit:
        return explicit
    base_url = (os.getenv("BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    return f"{base_url}/api/v1/calls/initiate"


def _twilio_voice_from_number() -> str:
    return (
        os.getenv("TWILIO_VOICE_FROM_NUMBER")
        or os.getenv("TWILIO_FROM_PHONE_NUMBER")
        or os.getenv("TWILIO_SMS_FROM_NUMBER")
        or os.getenv("TWILIO_FROM_NUMBER")
        or os.getenv("TWILIO_PHONE_NUMBER")
        or ""
    )


def _default_twilio_twiml_url() -> str:
    explicit = os.getenv("TWILIO_VOICE_TWIML_URL")
    if explicit:
        return explicit
    base_url = (
        os.getenv("TWILIO_WEBHOOK_BASE_URL") or os.getenv("BASE_URL") or ""
    ).rstrip("/")
    return f"{base_url}/api/v1/voice/twilio/twiml" if base_url else ""


def _default_twilio_media_stream_url() -> str:
    explicit = os.getenv("TWILIO_MEDIA_STREAM_URL")
    if explicit:
        return explicit
    base_url = (
        os.getenv("TWILIO_WEBHOOK_BASE_URL") or os.getenv("BASE_URL") or ""
    ).rstrip("/")
    if not base_url:
        return ""
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, "/api/v1/twilio/stream", "", ""))


def _twilio_stream_parameters(context: dict[str, Any], session_id: str) -> dict[str, str]:
    keys = (
        "source",
        "scenario",
        "clinic_id",
        "patient_id",
        "outreach_job_id",
        "record_call",
        "max_call_seconds",
        "demo_token",
    )
    params = {"session_id": session_id}
    for key in keys:
        value = context.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            params[key] = "true" if value else "false"
        else:
            params[key] = str(value)
    return params


def _url_with_query(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    query_items.extend(params.items())
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment)
    )


def _with_effect_token(url: str, effect_token: str | None) -> str:
    if not effect_token:
        return url
    parse_effect_token(effect_token)
    parts = urlsplit(url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == "effect_token" for key, _value in query_items):
        raise ValueError("Twilio callback URL must not predefine effect_token")
    query_items.append(("effect_token", effect_token))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query_items),
            parts.fragment,
        )
    )


def _twilio_connect_stream_twiml(stream_url: str, params: dict[str, str]) -> str:
    parameter_xml = "".join(
        f'<Parameter name="{_xml_attr(name)}" value="{_xml_attr(value)}" />'
        for name, value in params.items()
    )
    announcement = (
        f"<Say>{_xml_attr(RECORDING_ANNOUNCEMENT)}</Say>"
        if params.get("record_call") == "true"
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response>{announcement}<Connect><Stream url="{_xml_attr(stream_url)}">'
        f"{parameter_xml}"
        "</Stream></Connect></Response>"
    )


def _xml_attr(value: str) -> str:
    return xml_escape(value, {'"': "&quot;", "'": "&apos;"})


def _safe_json(response: httpx.Response) -> tuple[dict[str, Any], bool]:
    try:
        payload = response.json()
    except ValueError:
        return {}, False
    if not isinstance(payload, dict):
        return {}, False
    return payload, True


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _is_twilio_voice_configured() -> bool:
    return all([os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"), _twilio_voice_from_number()])


def _is_art_call_configured() -> bool:
    return bool(os.getenv("CLINIC_RECALL_CALL_INITIATE_URL") or os.getenv("BASE_URL"))