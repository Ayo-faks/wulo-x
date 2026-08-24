"""Twilio Voice webhooks for Clinic Recall fallback calls."""

from __future__ import annotations

import os
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response
from src.clinic_recall.db import clinic_scope, get_sessionmaker, tenant_select
from src.clinic_recall.demo_gate import (
    DemoGateError,
    demo_experience,
    phone_demo_enabled,
    verify_demo_token,
)
from src.clinic_recall.durable.callbacks import (
    CallbackCorrelationError,
    CallbackReceiptResult,
    CallbackValidationError,
    EffectTokenError,
    receive_twilio_callback,
    resolve_effect_token_clinic,
)
from src.clinic_recall.durable.config import callback_application_enabled
from src.clinic_recall.enums import (
    CallRecordingStatus,
    Channel,
    ClinicPhoneProvider,
    ExternalEffectState,
    ExternalEffectType,
    InteractionDirection,
    ProviderCallbackKind,
    ProviderCallbackState,
    RecordingConsentState,
)
from src.clinic_recall.inbound_transport import InboundRouteError, prepare_inbound_call
from src.clinic_recall.models import (
    CallRecord,
    ExternalEffect,
    OutreachJob,
    ProviderCallbackReceipt,
)
from src.clinic_recall.pilot_controls import (
    evaluate_patient_gate,
    evaluate_recording_gate,
    operational_switch_snapshot_from_environment,
)
from src.clinic_recall.recording import (
    RecordingBlobStore,
    RecordingConsentError,
    RecordingStoreError,
    ensure_call_record,
    finalize_call_transcript,
    mark_recording_consent_asked,
    mark_recording_failed,
    mark_recording_stored,
    record_recording_consent_evidence,
    recording_blob_path,
    recording_disclosure_from_environment,
    request_recording_start,
    resolve_call_record_clinic,
)
from src.clinic_recall.telemetry import emit_runtime_event
from src.clinic_recall.voice_worker import RECORDING_ANNOUNCEMENT
from utils.ml_logging import get_logger

from .sms import (
    _twilio_params_for_signature,
    _twilio_signature_required_for_request,
    _twilio_signature_url,
    _validate_twilio_signature,
)

logger = get_logger("api.v1.voice")
router = APIRouter()

_STREAM_PARAMETER_KEYS = (
    "session_id",
    "source",
    "scenario",
    "provider",
    "provider_call_id",
    "inbound_call_id",
    "call_direction",
    "called_number_id",
    "called_number",
    "caller_number_hash",
    "clinic_id",
    "patient_id",
    "outreach_job_id",
    "record_call",
    "max_call_seconds",
)

_DEMO_SOURCE = "clinic_recall_demo"
_DEMO_STREAM_PARAMETER_KEYS = ("session_id", "source", "scenario", "max_call_seconds")
_TWILIO_CALL_STATUSES = {
    "busy",
    "canceled",
    "completed",
    "failed",
    "in-progress",
    "no-answer",
    "queued",
    "ringing",
}
_TWILIO_TERMINAL_CALL_STATUSES = {"busy", "canceled", "completed", "failed", "no-answer"}


class _DurableCallbackError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("durable callback rejected")
        self.status_code = status_code


@router.api_route("/twilio/twiml", methods=["GET", "POST"], summary="Serve Twilio Voice TwiML")
async def handle_twilio_voice_twiml(request: Request) -> Response:
    """Return deterministic TwiML that connects Twilio audio to the Recall media bridge."""
    raw_payload = await request.body()
    form = await _request_form(request)
    params = _merged_params(request, form)

    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if account_sid and params.get("AccountSid") and params.get("AccountSid") != account_sid:
        logger.warning("Twilio Voice TwiML request rejected because account SID did not match")
        return JSONResponse({"status": "forbidden"}, status_code=403)

    if _twilio_signature_required_for_request(request):
        signature = request.headers.get("X-Twilio-Signature", "")
        signature_url = _twilio_signature_url(request)
        signature_params = _twilio_params_for_signature(request, form)
        if not _validate_twilio_signature(signature_url, signature_params, signature, auth_token):
            logger.warning("Twilio Voice TwiML request rejected because signature validation failed")
            return JSONResponse({"status": "unauthorized"}, status_code=401)

    durable_outbound = params.get("source") == "clinic_recall_voice_worker"
    durable_stream_params: dict[str, str] | None = None
    if durable_outbound:
        try:
            _persist_durable_callback(
                request,
                form=params,
                raw_payload=raw_payload,
                callback_kind=ProviderCallbackKind.AMD,
            )
        except _DurableCallbackError:
            logger.warning("Twilio AMD callback failed closed")
            return Response(
                content=_silent_hangup_twiml(),
                media_type="application/xml",
            )
        if params.get("AnsweredBy") != "human":
            return Response(
                content=_silent_hangup_twiml(),
                media_type="application/xml",
            )
        try:
            durable_stream_params = _durable_outbound_stream_parameters(params)
        except Exception:  # noqa: BLE001 - context failures must not expose internals
            logger.warning("Twilio durable outbound context failed closed")
            return Response(
                content=_silent_hangup_twiml(),
                media_type="application/xml",
            )
    elif params.get("AnsweredBy") and request.query_params.get("effect_token"):
        try:
            _persist_durable_callback(
                request,
                form=params,
                raw_payload=raw_payload,
                callback_kind=ProviderCallbackKind.AMD,
            )
        except _DurableCallbackError:
            logger.warning("Twilio AMD callback failed closed")
            return Response(content=_fail_closed_twiml(), media_type="application/xml")

    stream_url = _twilio_media_stream_url(request)
    if durable_stream_params is not None:
        stream_params = durable_stream_params
    elif params.get("source") == _DEMO_SOURCE:
        try:
            stream_params = _demo_stream_parameters(params)
        except DemoGateError as exc:
            logger.warning("Twilio demo call failed closed: %s", exc.reason)
            return Response(content=_fail_closed_twiml(), media_type="application/xml")
    else:
        try:
            stream_params = _trusted_stream_parameters(params)
        except InboundRouteError as exc:
            logger.warning("Twilio inbound call failed closed: %s", exc.reason)
            return Response(content=_fail_closed_twiml(), media_type="application/xml")
    consent_twiml = _recording_consent_gather_twiml(
        request,
        params=params,
        stream_params=stream_params,
    )
    if consent_twiml is not None:
        return Response(content=consent_twiml, media_type="application/xml")
    twiml = _connect_stream_twiml(stream_url, stream_params)
    logger.info(
        "Twilio Voice TwiML issued for call=%s stream_params=%s",
        "SET" if params.get("CallSid") else "missing",
        sorted(stream_params),
    )
    return Response(content=twiml, media_type="application/xml")


@router.post(
    "/twilio/recording-consent",
    summary="Twilio deterministic recording consent callback",
)
async def handle_twilio_recording_consent(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """Resolve one transient consent answer before opening the model stream."""
    form = await _request_form(request)
    params = _merged_params(request, form)
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if account_sid and params.get("AccountSid") and params.get("AccountSid") != account_sid:
        return JSONResponse({"status": "forbidden"}, status_code=403)
    signature = request.headers.get("X-Twilio-Signature", "")
    signature_url = _twilio_signature_url(request)
    signature_params = _twilio_params_for_signature(request, form)
    if not _validate_twilio_signature(signature_url, signature_params, signature, auth_token):
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    dispatch_recording = False
    try:
        stream_params = _recording_consent_stream_parameters(params)
        clinic_id = stream_params["clinic_id"]
        call_sid = str(params.get("CallSid") or "").strip()
        text = str(params.get("SpeechResult") or "").strip() or None
        dtmf = str(params.get("Digits") or "").strip() or None
        confidence = _recording_consent_confidence(params.get("Confidence"))
        source = "speech" if text is not None else "dtmf" if dtmf is not None else "timeout"
        SessionLocal = get_sessionmaker()
        with SessionLocal.begin() as session:
            with clinic_scope(session, clinic_id):
                record = session.execute(
                    tenant_select(CallRecord).where(
                        CallRecord.provider == ClinicPhoneProvider.TWILIO,
                        CallRecord.provider_call_id == call_sid,
                    )
                ).scalar_one_or_none()
                if record is None:
                    raise RecordingConsentError("call record not found for clinic")
                decided = record_recording_consent_evidence(
                    session,
                    clinic_id=clinic_id,
                    call_record_id=record.id,
                    text=text,
                    dtmf=dtmf,
                    confidence=confidence,
                    source=source,
                    now=datetime.now(UTC),
                )
                if (
                    decided.consent_state == RecordingConsentState.GRANTED
                    and decided.recording_status
                    in {
                        CallRecordingStatus.NONE,
                        CallRecordingStatus.ABSENT,
                        CallRecordingStatus.START_PENDING,
                    }
                ):
                    _effect, dispatch_recording = request_recording_start(
                        session,
                        clinic_id=clinic_id,
                        call_record_id=record.id,
                        now=datetime.now(UTC),
                    )
    except (InboundRouteError, RecordingConsentError, ValueError):
        logger.warning("Twilio recording consent failed closed")
        return Response(content=_fail_closed_twiml(), media_type="application/xml")

    if dispatch_recording:
        background_tasks.add_task(_dispatch_recording_effect_batch, clinic_id)
    stream_params["record_call"] = "false"
    return Response(
        content=_connect_stream_twiml(_twilio_media_stream_url(request), stream_params),
        media_type="application/xml",
    )


def _dispatch_recording_effect_batch(clinic_id: str) -> None:
    """Run one immediate finite batch after consent; pending work remains durable."""
    from src.clinic_recall.durable.recording_worker import run_runtime_batch

    try:
        result = run_runtime_batch(
            clinic_id=clinic_id,
            worker_id=f"recording-web-{uuid.uuid4().hex[:12]}",
            now=datetime.now(UTC),
            limit=1,
        )
        if result is None:
            return
        logger.info(
            "Recording effect batch completed: claimed=%d started=%d stopped=%d "
            "reconcile_required=%d",
            result.claimed,
            result.started,
            result.stopped,
            result.reconcile_required,
        )
    except Exception:  # noqa: BLE001 - committed pending work remains recoverable
        logger.error("Recording effect batch failed after durable enqueue")


@router.post("/twilio/call-status", summary="Twilio call status callback")
async def handle_twilio_call_status(request: Request) -> JSONResponse:
    """Capture a signed carrier disposition without retaining call identifiers."""
    raw_payload = await request.body()
    form = await _request_form(request)
    params = _merged_params(request, form)

    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if account_sid and params.get("AccountSid") and params.get("AccountSid") != account_sid:
        logger.warning("Twilio call-status callback rejected because account SID did not match")
        return JSONResponse({"status": "forbidden"}, status_code=403)

    signature = request.headers.get("X-Twilio-Signature", "")
    signature_url = _twilio_signature_url(request)
    signature_params = _twilio_params_for_signature(request, form)
    signature_valid = _validate_twilio_signature(
        signature_url,
        signature_params,
        signature,
        auth_token,
    )
    if _twilio_signature_required_for_request(request) and not signature_valid:
        logger.warning("Twilio call-status callback rejected because signature validation failed")
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    if request.query_params.get("effect_token"):
        try:
            callback_result = _persist_durable_callback(
                request,
                form=params,
                raw_payload=raw_payload,
                callback_kind=ProviderCallbackKind.VOICE,
            )
        except _DurableCallbackError as exc:
            logger.warning("Twilio call-status callback rejected durable evidence")
            return JSONResponse({"status": "rejected"}, status_code=exc.status_code)
    else:
        callback_result = None

    status = str(params.get("CallStatus") or "").strip().lower()
    if status not in _TWILIO_CALL_STATUSES:
        logger.info("Twilio call-status callback ignored unknown status")
        return JSONResponse({"status": "ignored"})

    if status in _TWILIO_TERMINAL_CALL_STATUSES and (
        callback_result is not None or signature_valid
    ):
        SessionLocal = get_sessionmaker()
        with SessionLocal.begin() as session:
            clinic_id = (
                _durable_callback_clinic_id(session, request)
                if callback_result is not None
                else resolve_call_record_clinic(
                    session,
                    provider=ClinicPhoneProvider.TWILIO,
                    provider_call_id=str(params.get("CallSid") or "").strip(),
                )
            )
            if clinic_id is not None:
                finalize_call_transcript(
                    session,
                    clinic_id=clinic_id,
                    provider=ClinicPhoneProvider.TWILIO,
                    provider_call_id=str(params.get("CallSid") or "").strip(),
                    transcript=None,
                    ended_at=datetime.now(UTC),
                )

    emit_runtime_event(
        "voice.call.status",
        {
            "provider": "twilio",
            "status": status,
            "answered": status in {"in-progress", "completed"},
            "terminal": status in _TWILIO_TERMINAL_CALL_STATUSES,
        },
    )
    logger.info(
        "Twilio aggregate call status captured: status=%s durable=%s",
        status,
        callback_result is not None,
    )
    return JSONResponse({"status": "accepted"})


@router.post("/twilio/recording-status", summary="Twilio recording status callback")
async def handle_twilio_recording_status(request: Request) -> JSONResponse:
    """Copy a completed consented recording into private blob storage.

    Pipeline (fails closed at every step):
    download from Twilio → upload to the tenant-prefixed blob path → update the
    call_record row → delete the Twilio-hosted copy (data residency). Any
    persistence failure returns 500 so Twilio retries the callback.
    """
    raw_payload = await request.body()
    form = await _request_form(request)
    params = _merged_params(request, form)

    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if account_sid and params.get("AccountSid") and params.get("AccountSid") != account_sid:
        logger.warning("Twilio recording callback rejected because account SID did not match")
        return JSONResponse({"status": "forbidden"}, status_code=403)

    if _twilio_signature_required_for_request(request):
        signature = request.headers.get("X-Twilio-Signature", "")
        signature_url = _twilio_signature_url(request)
        signature_params = _twilio_params_for_signature(request, form)
        if not _validate_twilio_signature(signature_url, signature_params, signature, auth_token):
            logger.warning("Twilio recording callback rejected because signature validation failed")
            return JSONResponse({"status": "unauthorized"}, status_code=401)

    status = str(params.get("RecordingStatus") or "").strip()
    call_sid = str(params.get("CallSid") or "").strip()
    recording_sid = str(params.get("RecordingSid") or "").strip()
    recording_url = str(params.get("RecordingUrl") or "").strip()
    validated_recording_url: str | None = None
    if status == "completed":
        if not (call_sid and recording_sid and recording_url):
            logger.warning("Twilio recording callback rejected missing evidence")
            return JSONResponse({"status": "rejected"}, status_code=400)
        try:
            validated_recording_url = _validated_twilio_recording_url(
                recording_url,
                account_sid=account_sid,
                recording_sid=recording_sid,
            )
        except RecordingDownloadError:
            logger.warning("Twilio recording callback rejected invalid media URL")
            return JSONResponse({"status": "rejected"}, status_code=400)
    SessionLocal = get_sessionmaker()
    callback_clinic_id: str | None = None
    clinic_id: str | None = None
    call_record_id: str | None = None
    callback_result: CallbackReceiptResult | None = None
    if request.query_params.get("effect_token"):
        try:
            with SessionLocal() as session:
                callback_clinic_id = _durable_callback_clinic_id(session, request)
                assert callback_clinic_id is not None  # nosec B101 - token is present
                call_record_id = _validate_recording_effect_context(
                    session,
                    clinic_id=callback_clinic_id,
                    effect_token=str(request.query_params.get("effect_token") or ""),
                    call_sid=call_sid,
                )
            clinic_id = callback_clinic_id
            callback_result = _persist_durable_callback(
                request,
                form=params,
                raw_payload=raw_payload,
                callback_kind=ProviderCallbackKind.RECORDING,
            )
        except EffectTokenError:
            logger.warning("Twilio recording callback rejected malformed correlation")
            return JSONResponse({"status": "rejected"}, status_code=400)
        except CallbackCorrelationError:
            logger.warning("Twilio recording callback rejected unknown correlation")
            return JSONResponse({"status": "rejected"}, status_code=404)
        except _DurableCallbackError as exc:
            logger.warning("Twilio recording callback rejected durable evidence")
            return JSONResponse({"status": "rejected"}, status_code=exc.status_code)

    if status != "completed":
        return JSONResponse({"status": "ignored", "recording_status": status})
    assert validated_recording_url is not None  # nosec B101 - completed status validated above

    duration_raw = str(params.get("RecordingDuration") or "").strip()
    duration_s = int(duration_raw) if duration_raw.isdigit() else None
    if clinic_id is None or call_record_id is None or callback_result is None:
        logger.warning("Recording callback has no durable recording authority; not storing")
        return JSONResponse({"status": "no_call_record"}, status_code=200)
    if callback_result.state == ProviderCallbackState.PENDING:
        return JSONResponse({"status": "retry"}, status_code=503)
    if callback_result.state != ProviderCallbackState.APPLIED:
        return JSONResponse({"status": "rejected"}, status_code=409)
    with SessionLocal() as session:
        if not _recording_storage_authorized(
            session,
            clinic_id=clinic_id,
            call_record_id=call_record_id,
            recording_sid=recording_sid,
        ):
            logger.warning("Recording callback failed durable authority checks")
            return JSONResponse({"status": "rejected"}, status_code=409)

    blob_path = recording_blob_path(clinic_id, call_sid, recording_sid)
    with SessionLocal() as session:
        already_stored = _recording_already_stored(
            session,
            clinic_id=clinic_id,
            call_record_id=call_record_id,
            recording_sid=recording_sid,
            blob_path=blob_path,
        )
    if already_stored:
        deleted = _delete_twilio_recording(recording_sid, account_sid, auth_token)
        if not deleted:
            return JSONResponse(
                {"status": "retry", "twilio_deleted": False},
                status_code=500,
            )
        return JSONResponse({"status": "stored", "twilio_deleted": True})
    try:
        audio = _download_twilio_recording(validated_recording_url, account_sid, auth_token)
        _recording_store().upload(blob_path, audio)
    except (RecordingStoreError, RecordingDownloadError) as exc:
        logger.error("Recording persistence failed: %s", type(exc).__name__)
        with SessionLocal.begin() as session:
            scope = (
                clinic_scope(session, callback_clinic_id)
                if callback_clinic_id is not None
                else nullcontext()
            )
            with scope:
                mark_recording_failed(
                    session,
                    clinic_id=clinic_id,
                    provider=ClinicPhoneProvider.TWILIO,
                    provider_call_id=call_sid,
                    recording_sid=recording_sid,
                )
        # 500 → Twilio retries the callback; the recording stays retrievable.
        return JSONResponse({"status": "retry"}, status_code=500)

    with SessionLocal.begin() as session:
        scope = (
            clinic_scope(session, callback_clinic_id)
            if callback_clinic_id is not None
            else nullcontext()
        )
        with scope:
            mark_recording_stored(
                session,
                clinic_id=clinic_id,
                provider=ClinicPhoneProvider.TWILIO,
                provider_call_id=call_sid,
                recording_sid=recording_sid,
                blob_path=blob_path,
                duration_s=duration_s,
            )

    deleted = _delete_twilio_recording(recording_sid, account_sid, auth_token)
    logger.info(
        "Recording stored; provider_copy_deleted=%s",
        deleted,
    )
    if not deleted:
        return JSONResponse(
            {"status": "retry", "twilio_deleted": False},
            status_code=500,
        )
    return JSONResponse({"status": "stored", "twilio_deleted": True})


def _validate_recording_effect_context(
    session,
    *,
    clinic_id: str,
    effect_token: str,
    call_sid: str,
) -> str:
    with clinic_scope(session, clinic_id):
        effect = session.execute(
            tenant_select(ExternalEffect).where(
                ExternalEffect.callback_token == effect_token,
                ExternalEffect.effect_type == ExternalEffectType.RECORDING,
            )
        ).scalar_one_or_none()
        record = session.execute(
            tenant_select(CallRecord).where(
                CallRecord.provider == ClinicPhoneProvider.TWILIO,
                CallRecord.provider_call_id == call_sid,
            )
        ).scalar_one_or_none()
        if effect is None or record is None:
            raise CallbackCorrelationError("recording callback context is unknown")
        expected_payload = {
            "intent": "recording_start",
            "call_record_id": record.id,
        }
        if (
            effect.aggregate_type != "call_record"
            or effect.aggregate_id != record.id
            or effect.payload_version != 1
            or effect.payload != expected_payload
            or record.consent_state
            not in {RecordingConsentState.GRANTED, RecordingConsentState.WITHDRAWN}
            or record.recording_requested_at is None
        ):
            raise CallbackCorrelationError("recording callback context does not match")
        return record.id


def _recording_storage_authorized(
    session,
    *,
    clinic_id: str,
    call_record_id: str,
    recording_sid: str,
) -> bool:
    with clinic_scope(session, clinic_id):
        record = session.execute(
            tenant_select(CallRecord).where(CallRecord.id == call_record_id)
        ).scalar_one_or_none()
        return bool(
            record is not None
            and record.consent_state
            in {RecordingConsentState.GRANTED, RecordingConsentState.WITHDRAWN}
            and record.recording_requested_at is not None
            and record.recording_sid == recording_sid
            and record.recording_status
            in {
                CallRecordingStatus.COMPLETED,
                CallRecordingStatus.FAILED,
                CallRecordingStatus.STORED,
            }
        )


def _recording_already_stored(
    session,
    *,
    clinic_id: str,
    call_record_id: str,
    recording_sid: str,
    blob_path: str,
) -> bool:
    with clinic_scope(session, clinic_id):
        record = session.execute(
            tenant_select(CallRecord).where(CallRecord.id == call_record_id)
        ).scalar_one_or_none()
        return bool(
            record is not None
            and record.recording_status == CallRecordingStatus.STORED
            and record.recording_sid == recording_sid
            and record.recording_blob_path == blob_path
        )


class RecordingDownloadError(Exception):
    """Raised when the Twilio-hosted recording cannot be fetched."""


def _validated_twilio_recording_url(
    recording_url: str,
    *,
    account_sid: str,
    recording_sid: str,
) -> str:
    """Allow only the exact configured Twilio recording resource URL."""
    base = os.getenv("TWILIO_API_BASE_URL", "https://api.twilio.com").rstrip("/")
    base_parts = urlsplit(base)
    recording_parts = urlsplit(recording_url)
    if (
        base_parts.scheme not in {"http", "https"}
        or not base_parts.netloc
        or base_parts.query
        or base_parts.fragment
        or base_parts.username
        or base_parts.password
        or recording_parts.scheme != base_parts.scheme
        or recording_parts.netloc != base_parts.netloc
        or recording_parts.query
        or recording_parts.fragment
        or recording_parts.username
        or recording_parts.password
    ):
        raise RecordingDownloadError("recording_url_not_allowed")
    expected_path = (
        f"{base_parts.path.rstrip('/')}/2010-04-01/Accounts/"
        f"{account_sid}/Recordings/{recording_sid}"
    )
    if recording_parts.path != expected_path:
        raise RecordingDownloadError("recording_url_not_allowed")
    return urlunsplit(
        (
            recording_parts.scheme,
            recording_parts.netloc,
            recording_parts.path,
            "",
            "",
        )
    )


def sa_select_call_record(call_sid: str, clinic_id: str | None = None):
    import sqlalchemy as sa
    from src.clinic_recall.models import CallRecord

    statement = sa.select(CallRecord).where(
        CallRecord.provider == ClinicPhoneProvider.TWILIO,
        CallRecord.provider_call_id == call_sid,
    )
    if clinic_id is not None:
        statement = statement.where(CallRecord.clinic_id == clinic_id)
    return statement


def _download_twilio_recording(recording_url: str, account_sid: str, auth_token: str) -> bytes:
    """Fetch the completed recording audio (WAV) from Twilio."""
    import httpx

    url = recording_url if recording_url.endswith(".wav") else f"{recording_url}.wav"
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, auth=(account_sid, auth_token))
    except httpx.HTTPError as exc:
        raise RecordingDownloadError(f"download_failed:{exc.__class__.__name__}") from exc
    if response.status_code >= 400 or not response.content:
        raise RecordingDownloadError(f"download_failed:{response.status_code}")
    return response.content


def _delete_twilio_recording(recording_sid: str, account_sid: str, auth_token: str) -> bool:
    """Best-effort delete of the Twilio-hosted copy after the blob copy exists."""
    import httpx

    base = os.getenv("TWILIO_API_BASE_URL", "https://api.twilio.com").rstrip("/")
    url = f"{base}/2010-04-01/Accounts/{account_sid}/Recordings/{recording_sid}.json"
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.delete(url, auth=(account_sid, auth_token))
    except httpx.HTTPError:
        logger.warning("Twilio recording delete failed", exc_info=True)
        return False
    return response.status_code < 400 or response.status_code == 404


def _recording_store() -> RecordingBlobStore:
    """Factory hook (monkeypatchable in tests)."""
    return RecordingBlobStore()


def _persist_durable_callback(
    request: Request,
    *,
    form: dict[str, str],
    raw_payload: bytes,
    callback_kind: ProviderCallbackKind,
) -> CallbackReceiptResult:
    try:
        SessionLocal = get_sessionmaker()
        with SessionLocal.begin() as session:
            return receive_twilio_callback(
                session,
                effect_token=str(request.query_params.get("effect_token") or ""),
                callback_kind=callback_kind,
                fields=form,
                raw_payload=raw_payload,
                received_at=datetime.now(UTC),
                apply_immediately=callback_application_enabled(),
            )
    except (EffectTokenError, CallbackValidationError) as exc:
        raise _DurableCallbackError(400) from exc
    except CallbackCorrelationError as exc:
        raise _DurableCallbackError(404) from exc
    except Exception as exc:  # noqa: BLE001 - never leak persistence details to provider
        logger.error("Twilio durable callback persistence failed")
        raise _DurableCallbackError(500) from exc


def _durable_callback_clinic_id(session, request: Request) -> str | None:
    effect_token = str(request.query_params.get("effect_token") or "")
    if not effect_token:
        return None
    return resolve_effect_token_clinic(session, effect_token)


async def _request_form(request: Request) -> dict[str, str]:
    if request.method.upper() != "POST":
        return {}
    return {key: str(value) for key, value in (await request.form()).items()}


def _merged_params(request: Request, form: dict[str, str]) -> dict[str, str]:
    params = dict(request.query_params)
    params.update(form)
    return params


def _stream_parameters(params: dict[str, str]) -> dict[str, str]:
    stream_params = {
        key: params[key] for key in _STREAM_PARAMETER_KEYS if params.get(key) is not None
    }
    source = str(stream_params.get("source") or "")
    scenario = str(stream_params.get("scenario") or "").strip().lower()
    if source.startswith("clinic_recall") or scenario in {"rebooking", "inbound_clinic"}:
        stream_params["record_call"] = "false"
    return stream_params


def _demo_stream_parameters(params: dict[str, str]) -> dict[str, str]:
    """Trusted parameters for a demo call. Requires a valid signed demo token.

    Demo sessions never carry clinic/patient context; the demo token is never
    forwarded to the media stream. Fails closed on any verification error and
    on the runtime experience/flag kill switches, so an in-flight token cannot
    open a media stream after the demo is disabled.
    """
    if demo_experience() == "off" or not phone_demo_enabled():
        raise DemoGateError("demo_disabled", status_code=403)
    verify_demo_token(str(params.get("demo_token") or ""), expected_kind="phone")
    stream_params = {
        key: params[key] for key in _DEMO_STREAM_PARAMETER_KEYS if params.get(key) is not None
    }
    stream_params["scenario"] = "demo"
    stream_params.setdefault("session_id", f"twilio-demo-{uuid.uuid4().hex}")
    return stream_params


def _trusted_stream_parameters(params: dict[str, str]) -> dict[str, str]:
    if not _should_prepare_inbound(params):
        return _stream_parameters(params)

    SessionLocal = get_sessionmaker()
    with SessionLocal.begin() as session:
        context = prepare_inbound_call(
            session,
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=str(params.get("CallSid") or "").strip(),
            called_number=str(params.get("To") or "").strip(),
            caller_number=str(params.get("From") or "").strip() or None,
            metadata=params,
            now=datetime.now(UTC),
        )
    stream_params = context.stream_parameters()
    stream_params["session_id"] = str(params.get("session_id") or context.inbound_call_id or f"twilio-session-{uuid.uuid4().hex}")
    return stream_params


def _durable_outbound_stream_parameters(params: dict[str, str]) -> dict[str, str]:
    effect_token = str(params.get("effect_token") or "")
    call_sid = str(params.get("CallSid") or "").strip()
    if (
        params.get("source") != "clinic_recall_voice_worker"
        or not effect_token
        or not call_sid
    ):
        raise ValueError("invalid durable outbound context")
    if params.get("scenario") not in {None, "rebooking"}:
        raise ValueError("invalid durable outbound context")
    if params.get("record_call") not in {None, "false"}:
        raise ValueError("invalid durable outbound context")

    SessionLocal = get_sessionmaker()
    with SessionLocal.begin() as session:
        clinic_id = resolve_effect_token_clinic(session, effect_token)
        with clinic_scope(session, clinic_id):
            effect = session.execute(
                tenant_select(ExternalEffect).where(
                    ExternalEffect.callback_token == effect_token,
                    ExternalEffect.effect_type == ExternalEffectType.CALL,
                    ExternalEffect.aggregate_type == "outreach_job",
                )
            ).scalar_one_or_none()
            if effect is None:
                raise ValueError("durable CALL effect not found")
            job = session.execute(
                tenant_select(OutreachJob).where(
                    OutreachJob.id == effect.aggregate_id,
                )
            ).scalar_one_or_none()
            expected_payload = {
                "intent": "recall_fallback",
                "outreach_job_id": effect.aggregate_id,
            }
            lifecycle_allows_stream = effect.state in {
                ExternalEffectState.DISPATCHING,
                ExternalEffectState.SUCCEEDED,
            } or (
                effect.state == ExternalEffectState.RECONCILE_REQUIRED
                and effect.last_error_class
                in {"ProviderDispatchError", "AmbiguousDispatch"}
            )
            amd_receipts = list(
                session.execute(
                    tenant_select(ProviderCallbackReceipt)
                    .with_only_columns(
                        ProviderCallbackReceipt.normalized_status,
                        ProviderCallbackReceipt.provider_resource_id,
                    )
                    .where(
                        ProviderCallbackReceipt.external_effect_id == effect.id,
                        ProviderCallbackReceipt.callback_kind
                        == ProviderCallbackKind.AMD,
                    )
                ).all()
            )
            amd_statuses = {row.normalized_status for row in amd_receipts}
            amd_call_sids = {
                row.provider_resource_id
                for row in amd_receipts
                if row.provider_resource_id is not None
            }
            pilot_decision = (
                evaluate_patient_gate(
                    session,
                    clinic_id=clinic_id,
                    patient_id=job.patient_id,
                    channel=Channel.CALL,
                    switches=operational_switch_snapshot_from_environment(),
                    now=datetime.now(UTC),
                )
                if job is not None
                else None
            )
            if (
                job is None
                or effect.payload_version != 1
                or effect.payload != expected_payload
                or not lifecycle_allows_stream
                or amd_statuses != {"human"}
                or amd_call_sids != {call_sid}
                or pilot_decision is None
                or not pilot_decision.allowed
                or params.get("clinic_id") not in {None, clinic_id}
                or params.get("outreach_job_id") not in {None, job.id}
                or params.get("patient_id") not in {None, job.patient_id}
                or (
                    effect.provider_resource_id is not None
                    and effect.provider_resource_id != call_sid
                )
            ):
                raise ValueError("durable outbound context mismatch")
            ensure_call_record(
                session,
                clinic_id,
                provider=ClinicPhoneProvider.TWILIO,
                provider_call_id=call_sid,
                external_effect_id=effect.id,
                session_id=str(params.get("session_id") or "") or None,
                direction=InteractionDirection.OUTBOUND,
                scenario="rebooking",
                patient_id=job.patient_id,
                consent_snapshot=None,
                now=datetime.now(UTC),
            )

    return {
        "session_id": str(params.get("session_id") or f"twilio-session-{uuid.uuid4().hex}"),
        "source": "clinic_recall_voice_worker",
        "scenario": "rebooking",
        "clinic_id": clinic_id,
        "patient_id": job.patient_id,
        "outreach_job_id": job.id,
        "record_call": "false",
    }


def _recording_consent_gather_twiml(
    request: Request,
    *,
    params: dict[str, str],
    stream_params: dict[str, str],
) -> str | None:
    source = str(stream_params.get("source") or "")
    if source not in {"clinic_recall_inbound", "clinic_recall_voice_worker"}:
        return None
    from src.clinic_recall.durable.config import (
        durable_recording_enabled,
        durable_recording_provider_is_twilio,
    )

    if not durable_recording_enabled() or not durable_recording_provider_is_twilio():
        return None
    disclosure = recording_disclosure_from_environment()
    clinic_id = str(stream_params.get("clinic_id") or "")
    call_sid = str(params.get("CallSid") or "").strip()
    if disclosure is None or not clinic_id or not call_sid:
        return None
    action = _recording_consent_action_url(params)
    if action is None:
        logger.warning("Twilio recording consent callback base is not configured for HTTPS")
        return None

    SessionLocal = get_sessionmaker()
    with SessionLocal.begin() as session:
        decision = evaluate_recording_gate(
            session,
            clinic_id=clinic_id,
            switches=operational_switch_snapshot_from_environment(),
            now=datetime.now(UTC),
        )
        if not decision.allowed:
            return None
        with clinic_scope(session, clinic_id):
            record = session.execute(
                tenant_select(CallRecord).where(
                    CallRecord.provider == ClinicPhoneProvider.TWILIO,
                    CallRecord.provider_call_id == call_sid,
                )
            ).scalar_one_or_none()
            if record is None:
                return None
            if record.consent_state == RecordingConsentState.NOT_ASKED:
                mark_recording_consent_asked(
                    session,
                    clinic_id=clinic_id,
                    call_record_id=record.id,
                    disclosure=disclosure,
                    source="twilio_gather",
                    now=datetime.now(UTC),
                )
            elif record.consent_state != RecordingConsentState.ASKED:
                return None

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Gather input="speech dtmf" numDigits="1" timeout="5" '
        'speechTimeout="auto" actionOnEmptyResult="true" method="POST" '
        f'action="{_xml_attr(action)}"><Say>{_xml_attr(disclosure.text)}</Say>'
        "</Gather></Response>"
    )


def _recording_consent_action_url(params: dict[str, str]) -> str | None:
    configured_base = os.getenv("TWILIO_WEBHOOK_BASE_URL") or os.getenv("BASE_URL") or ""
    parts = urlsplit(configured_base.strip())
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        return None
    base_url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    query: dict[str, str] = {}
    if params.get("source") == "clinic_recall_voice_worker":
        query["source"] = "clinic_recall_voice_worker"
        effect_token = str(params.get("effect_token") or "")
        if effect_token:
            query["effect_token"] = effect_token
    suffix = f"?{urlencode(query)}" if query else ""
    return f"{base_url}/api/v1/voice/twilio/recording-consent{suffix}"


def _recording_consent_stream_parameters(params: dict[str, str]) -> dict[str, str]:
    if params.get("source") == "clinic_recall_voice_worker":
        return _durable_outbound_stream_parameters(params)
    return _trusted_stream_parameters(params)


def _recording_consent_confidence(raw_value: str | None) -> float | None:
    if raw_value is None or not str(raw_value).strip():
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 1 else None


def _should_prepare_inbound(params: dict[str, str]) -> bool:
    source = str(params.get("source") or "").strip()
    if source == "clinic_recall_voice_worker":
        return False
    if source == "clinic_recall_inbound":
        return True
    return bool(params.get("To") and params.get("From") and params.get("CallSid") and not params.get("clinic_id"))


def _twilio_media_stream_url(request: Request) -> str:
    explicit = os.getenv("TWILIO_MEDIA_STREAM_URL")
    if explicit:
        return explicit

    base_url = os.getenv("TWILIO_WEBHOOK_BASE_URL") or os.getenv("BASE_URL")
    if not base_url:
        base_url = f"{request.url.scheme}://{request.url.netloc}"

    parts = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    base_path = parts.path.rstrip("/")
    stream_path = f"{base_path}/api/v1/twilio/stream" if base_path else "/api/v1/twilio/stream"
    return urlunsplit((scheme, parts.netloc, stream_path, "", ""))


def _connect_stream_twiml(stream_url: str, params: dict[str, str]) -> str:
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


def _fail_closed_twiml() -> str:
    message = "We are unable to connect this call right now. Please contact the clinic directly."
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{_xml_attr(message)}</Say><Hangup /></Response>"
    )


def _silent_hangup_twiml() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup /></Response>'


def _xml_attr(value: str) -> str:
    return xml_escape(value, {'"': "&quot;", "'": "&apos;"})
