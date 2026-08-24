"""
ACS SMS Event Grid endpoints for Phase 0 spike proofs.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import MultipleResultsFound
from src.clinic_recall.db import get_sessionmaker
from src.clinic_recall.durable.callbacks import (
    CallbackCorrelationError,
    CallbackValidationError,
    EffectTokenError,
    parse_effect_token,
    receive_twilio_callback,
)
from src.clinic_recall.durable.config import callback_application_enabled
from src.clinic_recall.enums import (
    Channel,
    ClinicPhoneProvider,
    InteractionIntent,
    ProviderCallbackKind,
)
from src.clinic_recall.identity_evidence import (
    IdentityAuthorizationContext,
    IdentityEvidenceService,
)
from src.clinic_recall.identity_runtime import runtime_identity_service
from src.clinic_recall.inbound_messages import handle_cold_inbound_sms
from src.clinic_recall.incidents import handle_sms_incident_report, parse_sms_incident_report
from src.clinic_recall.messaging.inbound import classify_intent, handle_inbound_reply
from src.clinic_recall.messaging.resolve import resolve_inbound_sms_route
from src.clinic_recall.rights import SubjectFrozenError
from src.clinic_recall.telemetry import emit_runtime_event
from twilio.request_validator import RequestValidator
from utils.ml_logging import get_logger

logger = get_logger("api.v1.sms")
router = APIRouter()

_VALIDATION_EVENT = "Microsoft.EventGrid.SubscriptionValidationEvent"
_SMS_RECEIVED_EVENT = "Microsoft.Communication.SMSReceived"
_TWILIO_EMPTY_RESPONSE = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>"
_TWILIO_DELIVERY_STATUSES = {
    "accepted",
    "canceled",
    "delivered",
    "failed",
    "queued",
    "read",
    "scheduled",
    "sending",
    "sent",
    "undelivered",
}
_EXPECTED_RECALL_REPLY_LOOKUP_ERRORS = {
    "inbound reply did not match a patient in this clinic",
    "inbound reply did not match an active outreach job",
}


def sms_identity_dependencies(
    _route: Any,
    _from_address: str,
    now: datetime,
) -> tuple[IdentityEvidenceService, IdentityAuthorizationContext | None]:
    """Return T0-only runtime identity dependencies until policy approval."""
    return runtime_identity_service(now), None


def _is_expected_recall_reply_error(exc: Exception) -> bool:
    return (
        isinstance(exc, (MultipleResultsFound, SubjectFrozenError))
        or str(exc) in _EXPECTED_RECALL_REPLY_LOOKUP_ERRORS
    )


def _as_event_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("eventType") or event.get("type") or "")


def _extract_subscription_validation_code(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if _event_type(event) != _VALIDATION_EVENT:
            continue
        data = event.get("data") or {}
        if isinstance(data, dict) and data.get("validationCode"):
            return str(data["validationCode"])
    return None


def _phone_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        phone_number = value.get("phoneNumber")
        if isinstance(phone_number, dict) and phone_number.get("value"):
            return str(phone_number["value"])
        if value.get("value"):
            return str(value["value"])
        if value.get("rawId"):
            return str(value["rawId"])
    return None


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _summarize_sms_event(event: dict[str, Any]) -> dict[str, Any]:
    data = _event_data(event)
    message = _sms_message(data)

    return {
        "event_type": _event_type(event),
        "event_id": event.get("id"),
        "from": _presence(_phone_value(data.get("from"))),
        "to": _presence(_phone_value(data.get("to"))),
        "message_length": len(message),
        "received_at": data.get("receivedTimestamp") or event.get("eventTime"),
    }


def _sms_message(data: dict[str, Any]) -> str:
    return str(
        data.get("message")
        or data.get("messageContent")
        or data.get("content")
        or ""
    )


def _parse_event_time(value: Any) -> datetime:
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        except ValueError:
            logger.warning("SMS webhook received unparsable timestamp")
    return datetime.now(UTC)


def _summarize_twilio_sms_form(form: dict[str, str]) -> dict[str, Any]:
    message = form.get("Body", "")
    return {
        "event_type": "twilio.sms.received",
        "provider": "twilio",
        "event_id": _presence(form.get("MessageSid") or form.get("SmsSid")),
        "account_sid": _presence(form.get("AccountSid")),
        "from": _presence(form.get("From")),
        "to": _presence(form.get("To")),
        "message_length": len(message),
        "status": form.get("SmsStatus") or form.get("MessageStatus"),
    }


def _record_twilio_delivery_update(form: dict[str, str]) -> bool:
    status = str(form.get("MessageStatus") or form.get("SmsStatus") or "").strip().lower()
    if form.get("Body", "").strip() or status not in _TWILIO_DELIVERY_STATUSES:
        return False
    emit_runtime_event(
        "sms.delivery.updated",
        {
            "provider": "twilio",
            "channel": "sms",
            "status": status,
            "successful": status in {"delivered", "read"},
        },
    )
    return True


def _is_twilio_delivery_callback(form: dict[str, str]) -> bool:
    return not form.get("Body", "").strip() and bool(
        form.get("MessageStatus") or form.get("SmsStatus")
    )


def _presence(value: Any) -> str:
    return "SET" if str(value or "").strip() else "missing"


def _route_clinic_recall_sms_reply(
    *,
    provider: ClinicPhoneProvider,
    provider_message_id: str | None,
    from_address: str | None,
    to_address: str | None,
    body: str,
    occurred_at: datetime,
) -> str | None:
    if not from_address or not to_address or not body:
        return None
    try:
        SessionLocal = get_sessionmaker()
    except Exception as exc:  # noqa: BLE001 - webhook ack should not depend on local DB config
        logger.info("Clinic Recall SMS routing skipped; data plane is not configured: %s", exc)
        return _safe_handoff_reply()

    with SessionLocal() as session:
        route = resolve_inbound_sms_route(session, provider=provider, inbound_number=to_address)
        if route is None:
            logger.info("Clinic Recall SMS routing skipped; no clinic owns inbound number")
            return _safe_handoff_reply()
        reply: str | None = ""
        deterministic_intent = classify_intent(body)
        # Anonymous incident reporting (deterministic keyword flow). Safety
        # always wins: urgent/clinical content falls through to escalation
        # below even if the caller used the REPORT keyword. The phone number
        # is never passed to the incident service, so it cannot be stored.
        if deterministic_intent not in {InteractionIntent.URGENT, InteractionIntent.CLINICAL}:
            incident_description = parse_sms_incident_report(body)
            if incident_description is not None:
                reply = handle_sms_incident_report(
                    session,
                    route.clinic_id,
                    description=incident_description,
                    now=occurred_at,
                )
                logger.info(
                    "Clinic Recall anonymous incident report via SMS: clinic=%s recorded=%s",
                    route.clinic_id,
                    bool(incident_description),
                )
                session.commit()
                return reply
        if deterministic_intent not in {
            InteractionIntent.URGENT,
            InteractionIntent.CLINICAL,
            InteractionIntent.OPT_OUT,
            InteractionIntent.DECLINE,
        }:
            if deterministic_intent == InteractionIntent.REBOOK:
                try:
                    handle_inbound_reply(
                        session,
                        clinic_id=route.clinic_id,
                        from_address=from_address,
                        channel=Channel.SMS,
                        body=body,
                        now=occurred_at,
                    )
                except (LookupError, MultipleResultsFound, SubjectFrozenError) as exc:
                    if not _is_expected_recall_reply_error(exc):
                        raise
            identity_service, identity_context = sms_identity_dependencies(
                route,
                from_address,
                occurred_at,
            )
            result = handle_cold_inbound_sms(
                session,
                route=route,
                provider_message_id=provider_message_id,
                from_address=from_address,
                body=body,
                now=occurred_at,
                identity_service=identity_service,
                identity_context=identity_context,
            )
            reply = result.reply_message or _cold_inbound_sms_reply(result.intent) or ""
            logger.info(
                "Clinic Recall safe SMS routed via text adapter: deterministic_intent=%s intent=%s stage=%s booked=%s reply=%s",
                deterministic_intent.value,
                result.intent,
                result.booking_stage or "none",
                result.booked,
                _presence(reply),
            )
            session.commit()
            return reply
        try:
            active_result = handle_inbound_reply(
                session,
                clinic_id=route.clinic_id,
                from_address=from_address,
                channel=Channel.SMS,
                body=body,
                now=occurred_at,
            )
            if active_result.intent == InteractionIntent.REBOOK:
                identity_service, identity_context = sms_identity_dependencies(
                    route,
                    from_address,
                    occurred_at,
                )
                result = handle_cold_inbound_sms(
                    session,
                    route=route,
                    provider_message_id=provider_message_id,
                    from_address=from_address,
                    body=body,
                    now=occurred_at,
                    identity_service=identity_service,
                    identity_context=identity_context,
                )
                reply = result.reply_message or _cold_inbound_sms_reply(result.intent) or ""
                logger.info(
                    "Clinic Recall active SMS booking routed: outreach_job_id=%s intent=%s stage=%s booked=%s reply=%s",
                    active_result.outreach_job_id,
                    result.intent,
                    result.booking_stage or "none",
                    result.booked,
                    _presence(reply),
                )
            elif active_result.intent in {
                InteractionIntent.URGENT,
                InteractionIntent.CLINICAL,
                InteractionIntent.QUESTION,
                InteractionIntent.UNCLEAR,
            }:
                reply = _cold_inbound_sms_reply(active_result.intent.value) or ""
                logger.info(
                    "Clinic Recall active SMS escalated: outreach_job_id=%s intent=%s reply=%s",
                    active_result.outreach_job_id,
                    active_result.intent.value,
                    _presence(reply),
                )
        except (LookupError, MultipleResultsFound, SubjectFrozenError) as exc:
            if not _is_expected_recall_reply_error(exc):
                raise
            identity_service, identity_context = sms_identity_dependencies(
                route,
                from_address,
                occurred_at,
            )
            result = handle_cold_inbound_sms(
                session,
                route=route,
                provider_message_id=provider_message_id,
                from_address=from_address,
                body=body,
                now=occurred_at,
                identity_service=identity_service,
                identity_context=identity_context,
            )
            reply = result.reply_message or _cold_inbound_sms_reply(result.intent) or ""
            logger.info(
                "Clinic Recall cold inbound SMS routed: intent=%s kind=%s stage=%s booked=%s reply=%s",
                result.intent,
                result.kind.value if result.kind else "none",
                result.booking_stage or "none",
                result.booked,
                _presence(reply),
            )
        session.commit()
        return reply


def _cold_inbound_sms_reply(intent: str | None) -> str | None:
    intent_value = str(intent or "").strip().lower()
    if intent_value == "booking_confirmed":
        return "You're booked. The clinic will send any further details if needed."
    if intent_value == "booking_intake":
        return "I can help with that. Is this for a new appointment, changing an existing one, or would you like a callback?"
    if intent_value == "chitchat":
        return "Hi, this is the clinic assistant. How can I help with appointments or clinic information?"
    if intent_value == "callback":
        return "Thanks. A member of the clinic team will call you back."
    if intent_value == "booking_request":
        return "Thanks. Your appointment request has been passed to the clinic team to review."
    if intent_value in {"urgent", "clinical", "complaint", "safeguarding", "distress"}:
        return (
            "Thanks. I have flagged this for the clinic team to follow up. "
            "If this is urgent or you are in immediate danger, call 999 or seek urgent help now."
        )
    if intent_value in {"unclear", "question", "identity_unclear"}:
        return _safe_handoff_reply()
    return None


def _safe_handoff_reply() -> str:
    return "Thanks for your message. A member of the clinic team will follow up."


def _twilio_sms_response(message: str | None = None) -> str:
    if not message:
        return _TWILIO_EMPTY_RESPONSE
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{xml_escape(message)}</Message></Response>"
    )


def _twilio_signature_url(request: Request) -> str:
    override_base_url = os.getenv("TWILIO_WEBHOOK_BASE_URL") or os.getenv("BASE_URL")
    if not override_base_url:
        if request.query_params.get("effect_token"):
            return ""
        return str(request.url)

    parts = urlsplit(override_base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        return ""
    base_path = parts.path.rstrip("/")
    path = f"{base_path}{request.url.path}"
    url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


def _twilio_params_for_signature(
    _request: Request,
    form: dict[str, str],
) -> dict[str, str]:
    return dict(form)


def _make_twilio_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    return RequestValidator(auth_token).compute_signature(url, params)


def _validate_twilio_signature(url: str, params: dict[str, str], signature: str, auth_token: str) -> bool:
    if not url or not signature or not auth_token:
        return False
    return RequestValidator(auth_token).validate(url, params, signature)


def _twilio_signature_required() -> bool:
    return os.getenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "false").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def _twilio_signature_required_for_request(request: Request) -> bool:
    return bool(request.query_params.get("effect_token")) or _twilio_signature_required()


@router.post("/events", summary="Handle ACS SMS Event Grid events")
async def handle_sms_events(request: Request) -> JSONResponse:
    """Capture ACS SMS Event Grid callbacks without logging message content."""
    payload = await request.json()
    events = _as_event_list(payload)

    validation_code = _extract_subscription_validation_code(events)
    if validation_code:
        logger.info("ACS SMS Event Grid subscription validation received")
        return JSONResponse({"validationResponse": validation_code})

    summaries = []
    routed_events = 0
    for event in events:
        if _event_type(event) != _SMS_RECEIVED_EVENT:
            continue
        data = _event_data(event)
        summaries.append(_summarize_sms_event(event))
        routed = _route_clinic_recall_sms_reply(
            provider=ClinicPhoneProvider.ACS,
            provider_message_id=str(event.get("id")) if event.get("id") else None,
            from_address=_phone_value(data.get("from")),
            to_address=_phone_value(data.get("to")),
            body=_sms_message(data),
            occurred_at=_parse_event_time(data.get("receivedTimestamp") or event.get("eventTime")),
        )
        if routed is not None:
            routed_events += 1

    if summaries:
        logger.info("ACS SMS webhook captured %d inbound event(s): %s", len(summaries), summaries)
    else:
        logger.info("ACS SMS webhook received %d non-SMS event(s)", len(events))

    return JSONResponse(
        {
            "status": "success",
            "processed_events": len(events),
            "routed_events": routed_events,
            "sms_events": summaries,
        }
    )


@router.post("/twilio", summary="Handle Twilio SMS webhooks")
async def handle_twilio_sms(request: Request) -> Response:
    """Capture Twilio inbound SMS callbacks without logging message content."""
    raw_payload = await request.body()
    form = {key: str(value) for key, value in (await request.form()).items()}

    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if account_sid and form.get("AccountSid") != account_sid:
        logger.warning("Twilio SMS webhook rejected because account SID did not match")
        return JSONResponse({"status": "forbidden"}, status_code=403)

    if _twilio_signature_required_for_request(request):
        signature = request.headers.get("X-Twilio-Signature", "")
        signature_url = _twilio_signature_url(request)
        signature_params = _twilio_params_for_signature(request, form)
        if not _validate_twilio_signature(signature_url, signature_params, signature, auth_token):
            logger.warning("Twilio SMS webhook rejected because signature validation failed")
            return JSONResponse({"status": "unauthorized"}, status_code=401)

    summary = _summarize_twilio_sms_form(form)
    if _is_twilio_delivery_callback(form):
        effect_token = str(request.query_params.get("effect_token") or "")
        try:
            parse_effect_token(effect_token)
            SessionLocal = get_sessionmaker()
            with SessionLocal.begin() as session:
                result = receive_twilio_callback(
                    session,
                    effect_token=effect_token,
                    callback_kind=ProviderCallbackKind.SMS,
                    fields=form,
                    raw_payload=raw_payload,
                    received_at=datetime.now(UTC),
                    apply_immediately=callback_application_enabled(),
                )
        except (EffectTokenError, CallbackValidationError):
            logger.warning("Twilio SMS delivery callback rejected invalid evidence")
            return Response(
                content=_TWILIO_EMPTY_RESPONSE,
                media_type="application/xml",
                status_code=400,
            )
        except CallbackCorrelationError:
            logger.warning("Twilio SMS delivery callback rejected unknown correlation")
            return Response(
                content=_TWILIO_EMPTY_RESPONSE,
                media_type="application/xml",
                status_code=404,
            )
        except Exception:  # noqa: BLE001 - provider response must not leak database internals
            logger.error("Twilio SMS delivery callback persistence failed")
            return Response(
                content=_TWILIO_EMPTY_RESPONSE,
                media_type="application/xml",
                status_code=500,
            )
        _record_twilio_delivery_update(form)
        logger.info(
            "Twilio SMS delivery receipt handled: status=%s created=%s state=%s",
            summary["status"],
            result.created,
            result.state.value,
        )
        return Response(content=_TWILIO_EMPTY_RESPONSE, media_type="application/xml")
    reply = _route_clinic_recall_sms_reply(
        provider=ClinicPhoneProvider.TWILIO,
        provider_message_id=form.get("MessageSid") or form.get("SmsSid"),
        from_address=form.get("From"),
        to_address=form.get("To"),
        body=form.get("Body", ""),
        occurred_at=datetime.now(UTC),
    )
    logger.info("Twilio SMS webhook captured inbound event: %s", summary)
    return Response(content=_twilio_sms_response(reply), media_type="application/xml")