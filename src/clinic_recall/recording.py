"""Call-recording consent gates and persistence for Clinic Recall.

Consent gates are deterministic (AGENTS.md §2). Persistence keeps audio bytes
in private blob storage only — never in the database, and never left on the
telephony provider once copied (UK data residency).
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import httpx
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .durable.enqueue import (
    enqueue_recording_start_effect,
    enqueue_recording_stop_effect,
)
from .enums import (
    AuditAction,
    CallRecordingStatus,
    ClinicPhoneProvider,
    ExternalEffectType,
    InteractionDirection,
    RecordingConsentSource,
    RecordingConsentState,
)
from .messaging.audit import audit_action
from .models import CallRecord, Clinic, ExternalEffect, InboundCall, Patient
from .rights import SubjectFrozenError, assert_patient_writable

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_DISCLOSURE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_CALL_SID = re.compile(r"CA[0-9a-fA-F]{32}\Z")
_RECORDING_SID = re.compile(r"RE[0-9a-fA-F]{32}\Z")
_AFFIRMATIVE = frozenset(
    {
        "yes",
        "yes please",
        "yes i agree",
        "i agree",
        "i consent",
        "you may record",
    }
)
_DECLINED = frozenset(
    {
        "no",
        "no thank you",
        "no thanks",
        "do not record",
        "dont record",
        "i do not consent",
    }
)
_FINAL_CONSENT_STATES = frozenset(
    {
        RecordingConsentState.GRANTED,
        RecordingConsentState.DECLINED,
        RecordingConsentState.AMBIGUOUS,
        RecordingConsentState.WITHDRAWN,
    }
)
_MAX_PROVIDER_IDENTITY_CLINICS = 1000


class RecordingConsentError(ValueError):
    """A per-call consent transition would violate a closed invariant."""


class CallRecordError(ValueError):
    """An all-call ledger identity would violate a closed invariant."""


class RecordingProviderDisposition(StrEnum):
    """Closed provider outcomes for one recording control request."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


class RecordingProviderReason(StrEnum):
    """Allowlisted reasons that never retain a provider response body."""

    PROVIDER_ACCEPTED = "provider_accepted"
    PROVIDER_REJECTED = "provider_rejected"
    TRANSPORT_ERROR = "transport_error"
    PROVIDER_SERVER_ERROR = "provider_server_error"
    MALFORMED_RESPONSE = "malformed_response"
    MISSING_RECORDING_SID = "missing_recording_sid"
    PROVIDER_IDENTITY_CONFLICT = "provider_identity_conflict"
    PROVIDER_STATE_UNCONFIRMED = "provider_state_unconfirmed"


@dataclass(frozen=True)
class RecordingProviderResult:
    """Minimized recording-provider evidence returned to the durable worker."""

    disposition: RecordingProviderDisposition
    reason: RecordingProviderReason
    recording_sid: str | None = None
    provider_status: str | None = None


@dataclass(frozen=True)
class RecordingSwitchOffResult:
    """Aggregate-only outcome from one operational recording stop sweep."""

    canceled_starts: int = 0
    stops_enqueued: int = 0
    reconcile_required: int = 0


class TwilioRecordingProvider:
    """Closed Twilio in-progress Call Recording REST adapter."""

    name = "twilio"

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 10.0,
        api_base_url: str | None = None,
    ) -> None:
        self._account_sid = account_sid.strip()
        self._auth_token = auth_token
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._api_base_url = (api_base_url or "https://api.twilio.com").rstrip("/")

    def start_recording(
        self,
        *,
        call_sid: str,
        callback_url: str,
    ) -> RecordingProviderResult:
        """Request dual-channel recording and return only closed evidence."""
        if not _CALL_SID.fullmatch(call_sid) or not callback_url.startswith("https://"):
            return _provider_rejected()
        response = self._post(
            f"/2010-04-01/Accounts/{quote(self._account_sid, safe='')}/"
            f"Calls/{quote(call_sid, safe='')}/Recordings.json",
            data={
                "RecordingChannels": "dual",
                "RecordingStatusCallback": callback_url,
                "RecordingStatusCallbackEvent": [
                    "in-progress",
                    "completed",
                    "absent",
                ],
            },
        )
        result = _classify_recording_response(response)
        if (
            result.disposition == RecordingProviderDisposition.ACCEPTED
            and result.provider_status != "in-progress"
        ):
            return RecordingProviderResult(
                disposition=RecordingProviderDisposition.AMBIGUOUS,
                reason=RecordingProviderReason.PROVIDER_STATE_UNCONFIRMED,
            )
        return result

    def stop_recording(
        self,
        *,
        call_sid: str,
        recording_sid: str,
    ) -> RecordingProviderResult:
        """Stop one exact provider recording without replaying ambiguity."""
        if not _CALL_SID.fullmatch(call_sid) or not _RECORDING_SID.fullmatch(recording_sid):
            return _provider_rejected()
        response = self._post(
            f"/2010-04-01/Accounts/{quote(self._account_sid, safe='')}/"
            f"Calls/{quote(call_sid, safe='')}/Recordings/"
            f"{quote(recording_sid, safe='')}.json",
            data={"Status": "stopped"},
        )
        result = _classify_recording_response(response)
        if (
            result.disposition == RecordingProviderDisposition.ACCEPTED
            and result.recording_sid != recording_sid
        ):
            return RecordingProviderResult(
                disposition=RecordingProviderDisposition.AMBIGUOUS,
                reason=RecordingProviderReason.PROVIDER_IDENTITY_CONFLICT,
            )
        if (
            result.disposition == RecordingProviderDisposition.ACCEPTED
            and result.provider_status not in {"stopped", "completed"}
        ):
            return RecordingProviderResult(
                disposition=RecordingProviderDisposition.AMBIGUOUS,
                reason=RecordingProviderReason.PROVIDER_STATE_UNCONFIRMED,
            )
        return result

    def _post(
        self,
        path: str,
        *,
        data: dict[str, str | list[str]],
    ) -> httpx.Response | None:
        try:
            with httpx.Client(
                base_url=self._api_base_url,
                auth=(self._account_sid, self._auth_token),
                transport=self._transport,
                timeout=self._timeout_seconds,
            ) as client:
                return client.post(path, data=data)
        except httpx.HTTPError:
            return None


def _classify_recording_response(
    response: httpx.Response | None,
) -> RecordingProviderResult:
    if response is None:
        return RecordingProviderResult(
            disposition=RecordingProviderDisposition.AMBIGUOUS,
            reason=RecordingProviderReason.TRANSPORT_ERROR,
        )
    if 400 <= response.status_code < 500:
        return _provider_rejected()
    if response.status_code >= 500 or not 200 <= response.status_code < 300:
        return RecordingProviderResult(
            disposition=RecordingProviderDisposition.AMBIGUOUS,
            reason=RecordingProviderReason.PROVIDER_SERVER_ERROR,
        )
    try:
        payload = response.json()
    except ValueError:
        return RecordingProviderResult(
            disposition=RecordingProviderDisposition.AMBIGUOUS,
            reason=RecordingProviderReason.MALFORMED_RESPONSE,
        )
    if not isinstance(payload, dict):
        return RecordingProviderResult(
            disposition=RecordingProviderDisposition.AMBIGUOUS,
            reason=RecordingProviderReason.MALFORMED_RESPONSE,
        )
    recording_sid = payload.get("sid")
    if not isinstance(recording_sid, str) or not _RECORDING_SID.fullmatch(recording_sid):
        return RecordingProviderResult(
            disposition=RecordingProviderDisposition.AMBIGUOUS,
            reason=RecordingProviderReason.MISSING_RECORDING_SID,
        )
    provider_status = payload.get("status")
    if provider_status not in {
        "in-progress",
        "paused",
        "stopped",
        "completed",
        "processing",
        "absent",
    }:
        provider_status = None
    return RecordingProviderResult(
        disposition=RecordingProviderDisposition.ACCEPTED,
        reason=RecordingProviderReason.PROVIDER_ACCEPTED,
        recording_sid=recording_sid,
        provider_status=provider_status,
    )


def _provider_rejected() -> RecordingProviderResult:
    return RecordingProviderResult(
        disposition=RecordingProviderDisposition.REJECTED,
        reason=RecordingProviderReason.PROVIDER_REJECTED,
    )


@dataclass(frozen=True)
class RecordingDisclosure:
    """Approved deterministic disclosure text and immutable version."""

    text: str
    version: str


def recording_disclosure_from_environment(
    environment: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> RecordingDisclosure | None:
    """Load approved disclosure wording; incomplete configuration stays off."""
    values = environment if environment is not None else os.environ
    if str(values.get("AZURE_APPCONFIG_ENDPOINT") or "").strip():
        refreshed_at = str(
            values.get("CLINIC_RECALL_RECORDING_DISCLOSURE_REFRESHED_AT") or ""
        ).strip()
        raw_max_age = str(
            values.get("CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS") or ""
        ).strip()
        try:
            refreshed = datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
            max_age_seconds = int(raw_max_age)
        except (TypeError, ValueError):
            return None
        current = now or datetime.now(UTC)
        if (
            refreshed.tzinfo is None
            or refreshed.utcoffset() is None
            or current.tzinfo is None
            or current.utcoffset() is None
            or not 1 <= max_age_seconds <= 3600
        ):
            return None
        age_seconds = (
            current.astimezone(UTC) - refreshed.astimezone(UTC)
        ).total_seconds()
        if not 0 <= age_seconds <= max_age_seconds:
            return None
    if str(values.get("CLINIC_RECALL_RECORDING_DISCLOSURE_APPROVED") or "").strip().lower() not in _TRUE_VALUES:
        return None
    text = str(values.get("CLINIC_RECALL_RECORDING_DISCLOSURE_TEXT") or "").strip()
    version = str(values.get("CLINIC_RECALL_RECORDING_DISCLOSURE_VERSION") or "").strip()
    if not 20 <= len(text) <= 500 or not _DISCLOSURE_VERSION.fullmatch(version):
        return None
    return RecordingDisclosure(text=text, version=version)


def parse_recording_consent(
    *,
    text: str | None,
    dtmf: str | None,
    confidence: float | None,
) -> RecordingConsentState:
    """Classify strict yes/no evidence without retaining the raw answer."""
    speech_state = _parse_speech_consent(text, confidence)
    dtmf_state = _parse_dtmf_consent(dtmf)
    states = {state for state in (speech_state, dtmf_state) if state is not None}
    if not states or RecordingConsentState.AMBIGUOUS in states or len(states) != 1:
        return RecordingConsentState.AMBIGUOUS
    return states.pop()


def mark_recording_consent_asked(
    session: Session,
    *,
    clinic_id: str,
    call_record_id: str,
    disclosure: RecordingDisclosure,
    source: str,
    now: datetime,
) -> CallRecord:
    """Persist the deterministic ask without storing disclosure text."""
    _require_aware(now)
    record = _load_call_record(session, clinic_id, call_record_id)
    _assert_call_record_writable(session, record)
    if record.consent_state == RecordingConsentState.ASKED:
        if record.consent_version != disclosure.version:
            raise RecordingConsentError("consent disclosure version conflict")
        return record
    if record.consent_state != RecordingConsentState.NOT_ASKED:
        raise RecordingConsentError("consent has already been decided")
    if not source.strip():
        raise RecordingConsentError("consent ask source is required")
    record.consent_state = RecordingConsentState.ASKED
    record.consent_asked_at = now
    record.consent_version = disclosure.version
    audit_recording_consent_state(session, record, now=now, source="asked")
    session.flush()
    return record


def record_recording_consent_evidence(
    session: Session,
    *,
    clinic_id: str,
    call_record_id: str,
    text: str | None,
    dtmf: str | None,
    confidence: float | None,
    source: str,
    now: datetime,
    correction: bool = False,
) -> CallRecord:
    """Persist only the closed consent result; raw evidence remains transient."""
    _require_aware(now)
    record = _load_call_record(session, clinic_id, call_record_id)
    _assert_call_record_writable(session, record)
    resolved_source = _consent_source(source, text=text, dtmf=dtmf)
    decision = parse_recording_consent(text=text, dtmf=dtmf, confidence=confidence)
    if record.consent_state in _FINAL_CONSENT_STATES:
        if not correction:
            if record.consent_state == decision:
                return record
            raise RecordingConsentError("consent decision conflict")
        if record.recording_requested_at is not None or record.recording_started_at is not None:
            raise RecordingConsentError("consent cannot be corrected after recording authority")
        if record.consent_state == RecordingConsentState.WITHDRAWN:
            raise RecordingConsentError("withdrawn consent is terminal")
    elif record.consent_state != RecordingConsentState.ASKED:
        raise RecordingConsentError("recording consent has not been asked")
    record.consent_state = decision
    record.consent_decided_at = now
    record.consent_decision_source = resolved_source
    audit_recording_consent_state(session, record, now=now, source=resolved_source.value)
    session.flush()
    return record


def withdraw_recording_consent(
    session: Session,
    *,
    clinic_id: str,
    call_record_id: str,
    source: str,
    now: datetime,
) -> CallRecord:
    """Persist deterministic withdrawal and queue one exact recording stop."""
    _require_aware(now)
    if source.strip().lower() != RecordingConsentSource.DTMF.value:
        raise RecordingConsentError("recording withdrawal source must be dtmf")
    record = _load_call_record(session, clinic_id, call_record_id)
    if record.consent_state == RecordingConsentState.WITHDRAWN:
        return record
    if record.consent_state != RecordingConsentState.GRANTED:
        raise RecordingConsentError("recording consent is not active")

    record.consent_state = RecordingConsentState.WITHDRAWN
    record.consent_decided_at = now
    record.consent_decision_source = RecordingConsentSource.DTMF
    record.recording_stop_requested_at = record.recording_stop_requested_at or now
    audit_recording_consent_state(session, record, now=now, source="dtmf")
    if record.recording_status == CallRecordingStatus.IN_PROGRESS:
        request_recording_stop(
            session,
            clinic_id=clinic_id,
            call_record_id=call_record_id,
            now=now,
        )
    elif record.recording_status == CallRecordingStatus.START_PENDING:
        from .durable.effects import cancel_undispatched_effects

        canceled = cancel_undispatched_effects(
            session,
            clinic_id=clinic_id,
            aggregate_type="call_record",
            aggregate_id=call_record_id,
            effect_type=ExternalEffectType.RECORDING,
            now=now,
            reason_code="recording_consent_withdrawn",
        )
        record.recording_status = (
            CallRecordingStatus.ABSENT
            if canceled
            else CallRecordingStatus.RECONCILE_REQUIRED
        )
    session.flush()
    return record


def audit_recording_consent_state(
    session: Session,
    record: CallRecord,
    *,
    now: datetime,
    source: str,
) -> None:
    audit_action(
        session,
        record.clinic_id,
        AuditAction.RECORDING_CONSENT,
        record.id,
        {
            "call_record_id": record.id,
            "consent_state": record.consent_state.value,
            "consent_source": source,
            "consent_version": record.consent_version,
            "occurred_at": now,
        },
        actor="system:recording-consent",
    )


def _parse_speech_consent(
    text: str | None,
    confidence: float | None,
) -> RecordingConsentState | None:
    if text is None:
        return None
    if confidence is None or confidence < 0.8:
        return RecordingConsentState.AMBIGUOUS
    normalized = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    normalized = " ".join(normalized.split())
    if normalized in _AFFIRMATIVE:
        return RecordingConsentState.GRANTED
    if normalized in _DECLINED:
        return RecordingConsentState.DECLINED
    return RecordingConsentState.AMBIGUOUS


def _parse_dtmf_consent(dtmf: str | None) -> RecordingConsentState | None:
    if dtmf is None or not dtmf.strip():
        return None
    if dtmf.strip() == "1":
        return RecordingConsentState.GRANTED
    if dtmf.strip() == "2":
        return RecordingConsentState.DECLINED
    return RecordingConsentState.AMBIGUOUS


def _consent_source(
    source: str,
    *,
    text: str | None,
    dtmf: str | None,
) -> RecordingConsentSource:
    normalized = source.strip().lower()
    if normalized == RecordingConsentSource.SPEECH.value and text is not None:
        return RecordingConsentSource.SPEECH
    if normalized == RecordingConsentSource.DTMF.value and dtmf is not None:
        return RecordingConsentSource.DTMF
    if normalized == RecordingConsentSource.TIMEOUT.value and text is None and not dtmf:
        return RecordingConsentSource.TIMEOUT
    if normalized == RecordingConsentSource.POLICY.value:
        return RecordingConsentSource.POLICY
    raise RecordingConsentError("invalid consent decision source")


def _load_call_record(session: Session, clinic_id: str, call_record_id: str) -> CallRecord:
    record = session.execute(
        select(CallRecord).where(
            CallRecord.clinic_id == clinic_id,
            CallRecord.id == call_record_id,
        )
    ).scalar_one_or_none()
    if record is None:
        raise RecordingConsentError("call record not found for clinic")
    return record


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise RecordingConsentError("now must be timezone-aware")


# ---------------------------------------------------------------------------
# All-call ledger persistence
# ---------------------------------------------------------------------------


def ensure_call_record(
    session: Session,
    clinic_id: str,
    *,
    provider: ClinicPhoneProvider,
    provider_call_id: str | None,
    external_effect_id: str | None = None,
    inbound_call_id: str | None = None,
    session_id: str | None,
    direction: InteractionDirection,
    scenario: str | None,
    patient_id: str | None,
    consent_snapshot: dict[str, Any] | None,
    now: datetime,
) -> CallRecord:
    """Create or return one minimized ledger row for every trusted call."""
    _require_aware(now)
    provider_call_id = str(provider_call_id or "").strip() or None
    external_effect_id = str(external_effect_id or "").strip() or None
    inbound_call_id = str(inbound_call_id or "").strip() or None
    if external_effect_id and inbound_call_id:
        raise CallRecordError("call record cannot bind both outbound and inbound anchors")
    if not any((provider_call_id, external_effect_id, inbound_call_id)):
        raise CallRecordError("call record requires a trusted call anchor")

    with clinic_scope(session, clinic_id):
        _validate_call_anchor(
            session,
            provider=provider,
            provider_call_id=provider_call_id,
            external_effect_id=external_effect_id,
            inbound_call_id=inbound_call_id,
        )
        if patient_id is not None:
            patient = session.execute(
                tenant_select(Patient).where(Patient.id == patient_id)
            ).scalar_one_or_none()
            if patient is None:
                raise CallRecordError("patient anchor is invalid")
        existing = _find_call_record(
            session,
            provider=provider,
            provider_call_id=provider_call_id,
            external_effect_id=external_effect_id,
            inbound_call_id=inbound_call_id,
        )
        if existing is not None:
            _ensure_call_record_identity(
                existing,
                provider=provider,
                provider_call_id=provider_call_id,
                external_effect_id=external_effect_id,
                inbound_call_id=inbound_call_id,
            )
            return existing
        if patient_id is not None:
            assert_patient_writable(session, clinic_id, patient_id)
        record = CallRecord(
            id=f"callrec-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            patient_id=patient_id,
            external_effect_id=external_effect_id,
            inbound_call_id=inbound_call_id,
            provider=provider,
            provider_call_id=provider_call_id,
            session_id=session_id,
            direction=direction,
            scenario=scenario,
            started_at=now,
            consent_state=RecordingConsentState.NOT_ASKED,
            recording_status=CallRecordingStatus.NONE,
            consent_snapshot=consent_snapshot,
        )
        if session.bind is None or session.bind.dialect.name != "postgresql":
            session.add(record)
            session.flush()
            return record
        savepoint = session.begin_nested()
        try:
            session.add(record)
            session.flush()
        except IntegrityError:
            savepoint.rollback()
            existing = _find_call_record(
                session,
                provider=provider,
                provider_call_id=provider_call_id,
                external_effect_id=external_effect_id,
                inbound_call_id=inbound_call_id,
            )
            if existing is None:
                raise
            _ensure_call_record_identity(
                existing,
                provider=provider,
                provider_call_id=provider_call_id,
                external_effect_id=external_effect_id,
                inbound_call_id=inbound_call_id,
            )
            return existing
        else:
            savepoint.commit()
            return record


def bind_call_record_provider_identity(
    session: Session,
    *,
    clinic_id: str,
    call_record_id: str,
    provider: ClinicPhoneProvider,
    provider_call_id: str,
) -> CallRecord:
    """Attach one exact provider Call SID without permitting rebinding."""
    provider_call_id = provider_call_id.strip()
    if not provider_call_id:
        raise CallRecordError("provider call identity is required")
    with clinic_scope(session, clinic_id):
        record = session.execute(
            tenant_select(CallRecord).where(CallRecord.id == call_record_id)
        ).scalar_one_or_none()
        if record is None:
            raise CallRecordError("call record not found for clinic")
        if record.provider != provider:
            raise CallRecordError("provider identity conflict")
        if record.provider_call_id is not None:
            if record.provider_call_id != provider_call_id:
                raise CallRecordError("provider call identity conflict")
            return record
        other = session.execute(
            tenant_select(CallRecord).where(
                CallRecord.provider == provider,
                CallRecord.provider_call_id == provider_call_id,
                CallRecord.id != record.id,
            )
        ).scalar_one_or_none()
        if other is not None:
            raise CallRecordError("provider call identity conflict")
        record.provider_call_id = provider_call_id
        session.flush()
        return record


def request_recording_start(
    session: Session,
    *,
    clinic_id: str,
    call_record_id: str,
    now: datetime,
) -> tuple[ExternalEffect, bool]:
    """Persist one consent-bound, non-retrying recording-start intent."""
    _require_aware(now)
    record = _load_call_record(session, clinic_id, call_record_id)
    if record.consent_state != RecordingConsentState.GRANTED:
        raise RecordingConsentError("recording consent must be granted for this call")
    if record.provider != ClinicPhoneProvider.TWILIO:
        raise RecordingConsentError("recording provider is not enabled")
    if not record.provider_call_id or not _CALL_SID.fullmatch(record.provider_call_id):
        raise RecordingConsentError("provider call identity is required")
    if record.recording_status not in {
        CallRecordingStatus.NONE,
        CallRecordingStatus.ABSENT,
        CallRecordingStatus.START_PENDING,
    }:
        raise RecordingConsentError("recording start is not available")
    effect, created = enqueue_recording_start_effect(
        session,
        clinic_id=clinic_id,
        call_record_id=call_record_id,
        available_at=now,
    )
    if created:
        record.recording_requested_at = now
        record.recording_status = CallRecordingStatus.START_PENDING
    session.flush()
    return effect, created


def request_recording_stop(
    session: Session,
    *,
    clinic_id: str,
    call_record_id: str,
    now: datetime,
) -> tuple[ExternalEffect, bool]:
    """Persist one exact, non-retrying recording-stop intent."""
    _require_aware(now)
    record = _load_call_record(session, clinic_id, call_record_id)
    if record.provider != ClinicPhoneProvider.TWILIO:
        raise RecordingConsentError("recording provider is not enabled")
    if not record.provider_call_id or not _CALL_SID.fullmatch(record.provider_call_id):
        raise RecordingConsentError("provider call identity is required")
    if not record.recording_sid or not _RECORDING_SID.fullmatch(record.recording_sid):
        raise RecordingConsentError("recording identity is required")
    if record.recording_status not in {
        CallRecordingStatus.IN_PROGRESS,
        CallRecordingStatus.STOP_PENDING,
        CallRecordingStatus.RECONCILE_REQUIRED,
    }:
        raise RecordingConsentError("recording stop is not available")
    effect, created = enqueue_recording_stop_effect(
        session,
        clinic_id=clinic_id,
        call_record_id=call_record_id,
        available_at=now,
    )
    if created:
        record.recording_stop_requested_at = now
        record.recording_status = CallRecordingStatus.STOP_PENDING
    session.flush()
    return effect, created


def enforce_recording_switch_off(
    session: Session,
    *,
    clinic_id: str,
    now: datetime,
    reason_code: str,
) -> RecordingSwitchOffResult:
    """Cancel undispatched starts and queue exact-SID stops for one clinic."""
    _require_aware(now)
    if not _REASON_CODE.fullmatch(reason_code):
        raise RecordingConsentError("recording switch-off reason is invalid")
    with clinic_scope(session, clinic_id):
        statement = tenant_select(CallRecord).where(
            CallRecord.recording_status.in_(
                {
                    CallRecordingStatus.START_PENDING,
                    CallRecordingStatus.STARTING,
                    CallRecordingStatus.IN_PROGRESS,
                    CallRecordingStatus.RECONCILE_REQUIRED,
                }
            )
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        records = list(session.execute(statement).scalars())

        canceled_starts = 0
        stops_enqueued = 0
        reconcile_required = 0
        for record in records:
            record.recording_stop_requested_at = record.recording_stop_requested_at or now
            if record.recording_status == CallRecordingStatus.START_PENDING:
                from .durable.effects import cancel_undispatched_effects

                canceled = cancel_undispatched_effects(
                    session,
                    clinic_id=clinic_id,
                    aggregate_type="call_record",
                    aggregate_id=record.id,
                    effect_type=ExternalEffectType.RECORDING,
                    now=now,
                    reason_code=reason_code,
                )
                if canceled:
                    record.recording_status = CallRecordingStatus.ABSENT
                    canceled_starts += canceled
                else:
                    record.recording_status = CallRecordingStatus.RECONCILE_REQUIRED
                    reconcile_required += 1
                continue
            if record.recording_sid and _RECORDING_SID.fullmatch(record.recording_sid):
                _effect, created = request_recording_stop(
                    session,
                    clinic_id=clinic_id,
                    call_record_id=record.id,
                    now=now,
                )
                stops_enqueued += int(created)
            else:
                record.recording_status = CallRecordingStatus.RECONCILE_REQUIRED
                reconcile_required += 1
        session.flush()
        return RecordingSwitchOffResult(
            canceled_starts=canceled_starts,
            stops_enqueued=stops_enqueued,
            reconcile_required=reconcile_required,
        )


def _validate_call_anchor(
    session: Session,
    *,
    provider: ClinicPhoneProvider,
    provider_call_id: str | None,
    external_effect_id: str | None,
    inbound_call_id: str | None,
) -> None:
    if external_effect_id:
        effect = session.execute(
            tenant_select(ExternalEffect).where(ExternalEffect.id == external_effect_id)
        ).scalar_one_or_none()
        if effect is None or effect.effect_type != ExternalEffectType.CALL:
            raise CallRecordError("outbound call anchor is invalid")
    if inbound_call_id:
        inbound = session.execute(
            tenant_select(InboundCall).where(InboundCall.id == inbound_call_id)
        ).scalar_one_or_none()
        if inbound is None:
            raise CallRecordError("inbound call anchor is invalid")
        if inbound.provider != provider:
            raise CallRecordError("inbound call provider identity conflict")
        if provider_call_id is not None and inbound.provider_call_id != provider_call_id:
            raise CallRecordError("inbound provider call identity conflict")


def _find_call_record(
    session: Session,
    *,
    provider: ClinicPhoneProvider,
    provider_call_id: str | None,
    external_effect_id: str | None,
    inbound_call_id: str | None,
) -> CallRecord | None:
    conditions = []
    if external_effect_id:
        conditions.append(CallRecord.external_effect_id == external_effect_id)
    if inbound_call_id:
        conditions.append(CallRecord.inbound_call_id == inbound_call_id)
    if provider_call_id:
        conditions.append(
            sa.and_(
                CallRecord.provider == provider,
                CallRecord.provider_call_id == provider_call_id,
            )
        )
    if not conditions:
        return None
    records = list(
        session.execute(
            tenant_select(CallRecord).where(sa.or_(*conditions))
        ).scalars()
    )
    if len({record.id for record in records}) > 1:
        raise CallRecordError("call ledger anchors resolve to different rows")
    return records[0] if records else None


def _ensure_call_record_identity(
    record: CallRecord,
    *,
    provider: ClinicPhoneProvider,
    provider_call_id: str | None,
    external_effect_id: str | None,
    inbound_call_id: str | None,
) -> None:
    if record.provider != provider:
        raise CallRecordError("provider identity conflict")
    if provider_call_id and record.provider_call_id not in {None, provider_call_id}:
        raise CallRecordError("provider call identity conflict")
    if external_effect_id and record.external_effect_id not in {None, external_effect_id}:
        raise CallRecordError("outbound call anchor conflict")
    if inbound_call_id and record.inbound_call_id not in {None, inbound_call_id}:
        raise CallRecordError("inbound call anchor conflict")
    record.provider_call_id = record.provider_call_id or provider_call_id
    record.external_effect_id = record.external_effect_id or external_effect_id
    record.inbound_call_id = record.inbound_call_id or inbound_call_id


def finalize_call_transcript(
    session: Session,
    *,
    clinic_id: str,
    provider: ClinicPhoneProvider,
    provider_call_id: str,
    transcript: list[dict[str, Any]] | None,
    ended_at: datetime,
) -> bool:
    """Attach minimized turns to one tenant-bound provider-confirmed recording."""
    with clinic_scope(session, clinic_id):
        record = session.execute(
            tenant_select(CallRecord).where(
                CallRecord.provider == provider,
                CallRecord.provider_call_id == provider_call_id,
            )
        ).scalar_one_or_none()
    if record is None:
        return False
    record.ended_at = record.ended_at or ended_at
    if transcript is not None:
        try:
            _assert_call_record_writable(session, record)
        except SubjectFrozenError:
            return True
        if record.recording_status not in {
            CallRecordingStatus.IN_PROGRESS,
            CallRecordingStatus.COMPLETED,
            CallRecordingStatus.STORED,
        }:
            return False
        record.transcript = transcript
    return True


def _assert_call_record_writable(session: Session, record: CallRecord) -> None:
    if record.patient_id is not None:
        assert_patient_writable(session, record.clinic_id, record.patient_id)


def resolve_call_record_clinic(
    session: Session,
    *,
    provider: ClinicPhoneProvider,
    provider_call_id: str,
) -> str | None:
    """Resolve one signed provider identity across bounded tenant scopes."""
    clinic_ids = list(
        session.execute(
            select(Clinic.id).order_by(Clinic.id).limit(_MAX_PROVIDER_IDENTITY_CLINICS + 1)
        ).scalars()
    )
    if len(clinic_ids) > _MAX_PROVIDER_IDENTITY_CLINICS:
        raise CallRecordError("provider identity clinic scope exceeds the supported bound")
    matches: list[str] = []
    for clinic_id in clinic_ids:
        with clinic_scope(session, clinic_id):
            found = session.execute(
                tenant_select(CallRecord)
                .with_only_columns(CallRecord.id)
                .where(
                    CallRecord.provider == provider,
                    CallRecord.provider_call_id == provider_call_id,
                )
            ).first()
            if found is not None:
                matches.append(clinic_id)
    if len(matches) > 1:
        raise CallRecordError("provider call identity is ambiguous")
    return matches[0] if matches else None


def mark_recording_stored(
    session: Session,
    *,
    clinic_id: str,
    provider: ClinicPhoneProvider,
    provider_call_id: str,
    recording_sid: str,
    blob_path: str,
    duration_s: int | None,
) -> CallRecord | None:
    """Record a successful blob copy for the call's recording."""
    record = session.execute(
        select(CallRecord).where(
            CallRecord.clinic_id == clinic_id,
            CallRecord.provider == provider,
            CallRecord.provider_call_id == provider_call_id,
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    record.recording_sid = recording_sid
    record.recording_blob_path = blob_path
    record.recording_duration_s = duration_s
    record.recording_status = CallRecordingStatus.STORED
    return record


def mark_recording_failed(
    session: Session,
    *,
    clinic_id: str,
    provider: ClinicPhoneProvider,
    provider_call_id: str,
    recording_sid: str | None,
) -> CallRecord | None:
    """Record a failed recording pipeline for the call."""
    record = session.execute(
        select(CallRecord).where(
            CallRecord.clinic_id == clinic_id,
            CallRecord.provider == provider,
            CallRecord.provider_call_id == provider_call_id,
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    record.recording_sid = recording_sid or record.recording_sid
    record.recording_status = CallRecordingStatus.FAILED
    return record


# ---------------------------------------------------------------------------
# Blob storage for recording audio
# ---------------------------------------------------------------------------


class RecordingStoreError(Exception):
    """Raised when the recording blob store is unavailable or rejects a write."""


class RecordingBlobStore:
    """Uploads recording audio to a private Azure Blob container.

    Configuration (either style):
    - ``RECORDINGS_BLOB_CONNECTION_STRING`` (local dev / Azurite), or
    - ``RECORDINGS_BLOB_ACCOUNT_URL`` + DefaultAzureCredential (production).
    Container: ``RECORDINGS_BLOB_CONTAINER`` (default ``call-recordings``).
    """

    def __init__(
        self,
        *,
        connection_string: str | None = None,
        account_url: str | None = None,
        container: str | None = None,
    ) -> None:
        self.connection_string = (
            connection_string
            if connection_string is not None
            else os.getenv("RECORDINGS_BLOB_CONNECTION_STRING", "")
        )
        self.account_url = (
            account_url if account_url is not None else os.getenv("RECORDINGS_BLOB_ACCOUNT_URL", "")
        )
        self.container = container or os.getenv("RECORDINGS_BLOB_CONTAINER", "call-recordings")

    @property
    def configured(self) -> bool:
        return bool(self.connection_string or self.account_url)

    def _service_client(self):
        # Imported lazily so offline tests never need the Azure SDK.
        from azure.storage.blob import BlobServiceClient

        if self.connection_string:
            return BlobServiceClient.from_connection_string(self.connection_string)
        from azure.identity import DefaultAzureCredential

        return BlobServiceClient(account_url=self.account_url, credential=DefaultAzureCredential())

    def upload(self, blob_path: str, data: bytes, *, content_type: str = "audio/wav") -> str:
        """Upload recording bytes; returns the blob path. Fails closed."""
        if not self.configured:
            raise RecordingStoreError("recording_store_not_configured")
        try:
            from azure.storage.blob import ContentSettings

            service = self._service_client()
            blob_client = service.get_blob_client(container=self.container, blob=blob_path)
            blob_client.upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )
        except Exception as exc:  # noqa: BLE001 — any storage failure fails closed
            raise RecordingStoreError(f"recording_upload_failed:{exc.__class__.__name__}") from exc
        return blob_path


def recording_blob_path(clinic_id: str, provider_call_id: str, recording_sid: str) -> str:
    """Deterministic, tenant-prefixed blob path for a recording."""
    return f"{clinic_id}/{provider_call_id}/{recording_sid}.wav"