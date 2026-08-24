"""Minimized provider callback ingestion and monotonic application."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import clinic_scope, tenant_select
from ..enums import (
    CallRecordingStatus,
    ExternalEffectState,
    ExternalEffectType,
    OutreachState,
    ProviderCallbackKind,
    ProviderCallbackReason,
    ProviderCallbackState,
    RecordingConsentState,
)
from ..models import CallRecord, Clinic, ExternalEffect, OutreachJob, ProviderCallbackReceipt
from ..telemetry import emit_worker_summary

_TOKEN_VERSION = "cr2"
_TOKEN_MAX_LENGTH = 240
_CLINIC_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TOKEN_SCOPE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RANDOM_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_TOKEN_SCOPE_DOMAIN = b"clinic-recall-effect-scope-v1\0"
_MAX_TOKEN_SCOPE_CLINICS = 1000
_PAYLOAD_MAX_BYTES = 128 * 1024
_MAX_CALLBACK_FIELDS = 64
_MAX_CALLBACK_FIELD_NAME = 64
_MAX_CALLBACK_FIELD_VALUE = 16 * 1024
_MESSAGE_SID_PATTERN = re.compile(r"(?:SM|MM)[0-9a-fA-F]{32}\Z")
_CALL_SID_PATTERN = re.compile(r"CA[0-9a-fA-F]{32}\Z")
_RECORDING_SID_PATTERN = re.compile(r"RE[0-9a-fA-F]{32}\Z")

_SMS_GRAPH: dict[str, frozenset[str]] = {
    "accepted": frozenset({"scheduled", "queued", "canceled"}),
    "scheduled": frozenset({"queued", "canceled", "failed"}),
    "queued": frozenset({"sending", "sent", "canceled", "failed"}),
    "sending": frozenset({"sent", "failed", "undelivered"}),
    "sent": frozenset({"delivered", "failed", "undelivered"}),
    "delivered": frozenset({"read"}),
    "read": frozenset(),
    "failed": frozenset(),
    "undelivered": frozenset(),
    "canceled": frozenset(),
}
_VOICE_STATUSES = frozenset(
    {
        "queued",
        "initiated",
        "ringing",
        "in-progress",
        "completed",
        "busy",
        "failed",
        "no-answer",
        "canceled",
    }
)
_RECORDING_GRAPH: dict[str, frozenset[str]] = {
    "in-progress": frozenset({"completed", "absent"}),
    "completed": frozenset(),
    "absent": frozenset(),
}
_AMD_STATUSES = frozenset({"human", "machine_start", "fax", "unknown"})

_SUCCESS_STATUSES: dict[ProviderCallbackKind, frozenset[str]] = {
    ProviderCallbackKind.SMS: frozenset({"delivered", "read"}),
    ProviderCallbackKind.VOICE: frozenset({"completed"}),
    ProviderCallbackKind.RECORDING: frozenset({"completed"}),
    ProviderCallbackKind.AMD: frozenset({"human"}),
}
_FAILURE_STATUSES: dict[ProviderCallbackKind, frozenset[str]] = {
    ProviderCallbackKind.SMS: frozenset({"failed", "undelivered", "canceled"}),
    ProviderCallbackKind.VOICE: frozenset({"busy", "failed", "no-answer", "canceled"}),
    ProviderCallbackKind.RECORDING: frozenset({"absent"}),
    ProviderCallbackKind.AMD: frozenset({"machine_start", "fax"}),
}
_EXPECTED_EFFECT_TYPES = {
    ProviderCallbackKind.SMS: ExternalEffectType.SMS,
    ProviderCallbackKind.VOICE: ExternalEffectType.CALL,
    ProviderCallbackKind.RECORDING: ExternalEffectType.RECORDING,
    ProviderCallbackKind.AMD: ExternalEffectType.CALL,
}


class EffectTokenError(ValueError):
    """Raised when an effect token cannot be generated or parsed safely."""


class CallbackValidationError(ValueError):
    """Raised when provider fields cannot be normalized safely."""


class CallbackCorrelationError(LookupError):
    """Raised when a valid callback cannot resolve one scoped effect."""


@dataclass(frozen=True)
class ParsedEffectToken:
    """Strictly decoded routing metadata from an opaque effect token."""

    scope_id: str


@dataclass(frozen=True)
class NormalizedCallback:
    """Allowlisted callback fields after provider-specific normalization."""

    provider: str
    callback_kind: ProviderCallbackKind
    provider_resource_id: str | None
    normalized_status: str
    provider_sequence: int | None
    provider_observed_at: datetime | None
    contract_identity: tuple[str, ...] | None


@dataclass(frozen=True)
class CallbackReceiptResult:
    """Identifier-only result safe for endpoint responses and counters."""

    receipt_id: str
    created: bool
    state: ProviderCallbackState
    reason_code: ProviderCallbackReason | None


@dataclass(frozen=True)
class ReconciliationResult:
    """Aggregate-only outcome from one finite reconciliation invocation."""

    enabled: bool
    claimed: int = 0
    applied: int = 0
    conflicts: int = 0
    pending: int = 0
    unresolved_effects: int = 0

    def as_summary(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "claimed": self.claimed,
            "applied": self.applied,
            "conflicts": self.conflicts,
            "pending": self.pending,
            "unresolved_effects": self.unresolved_effects,
        }


def generate_effect_token(clinic_id: str) -> str:
    """Create a versioned, non-PII callback correlation token."""
    scope_id = effect_token_scope_id(clinic_id)
    token = f"{_TOKEN_VERSION}.{scope_id}.{secrets.token_urlsafe(32)}"
    if len(token) > _TOKEN_MAX_LENGTH:
        raise EffectTokenError("effect token inputs exceed the supported size")
    return token


def parse_effect_token(token: str) -> ParsedEffectToken:
    """Parse a bounded effect token without treating it as authentication."""
    if not token or len(token) > _TOKEN_MAX_LENGTH:
        raise EffectTokenError("invalid effect token")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
        raise EffectTokenError("invalid effect token")
    scope_id, random_segment = parts[1], parts[2]
    if not _TOKEN_SCOPE_PATTERN.fullmatch(scope_id) or not _TOKEN_RANDOM_PATTERN.fullmatch(
        random_segment
    ):
        raise EffectTokenError("invalid effect token")
    return ParsedEffectToken(scope_id=scope_id)


def effect_token_scope_id(clinic_id: str) -> str:
    """Return a domain-separated one-way routing digest for a clinic ID."""
    _validate_clinic_id(clinic_id)
    return hashlib.sha256(_TOKEN_SCOPE_DOMAIN + clinic_id.encode("utf-8")).hexdigest()


def resolve_effect_token_clinic(session: Session, effect_token: str) -> str:
    """Resolve one opaque scope digest without exposing tenant rows."""
    parsed = parse_effect_token(effect_token)
    clinic_ids = list(
        session.execute(
            sa.select(Clinic.id).order_by(Clinic.id).limit(_MAX_TOKEN_SCOPE_CLINICS + 1)
        ).scalars()
    )
    if len(clinic_ids) > _MAX_TOKEN_SCOPE_CLINICS:
        raise CallbackCorrelationError("callback clinic scope exceeds the supported bound")
    matches = [
        clinic_id
        for clinic_id in clinic_ids
        if hmac.compare_digest(effect_token_scope_id(clinic_id), parsed.scope_id)
    ]
    if len(matches) != 1:
        raise CallbackCorrelationError("callback clinic scope is unknown or ambiguous")
    return matches[0]


def receive_twilio_callback(
    session: Session,
    *,
    effect_token: str,
    callback_kind: ProviderCallbackKind,
    fields: Mapping[str, str],
    raw_payload: bytes,
    received_at: datetime,
    apply_immediately: bool = True,
) -> CallbackReceiptResult:
    """Normalize, deduplicate, and apply one already-verified Twilio callback."""
    _require_aware(received_at, "received_at")
    if len(raw_payload) > _PAYLOAD_MAX_BYTES:
        raise CallbackValidationError("callback payload exceeds the supported size")
    payload_hash = hashlib.sha256(raw_payload).hexdigest()
    normalized = normalize_twilio_callback(callback_kind, fields)
    clinic_id = resolve_effect_token_clinic(session, effect_token)
    token_hash = hashlib.sha256(effect_token.encode("utf-8")).hexdigest()

    with clinic_scope(session, clinic_id):
        effect = session.execute(
            tenant_select(ExternalEffect).where(
                ExternalEffect.callback_token == effect_token,
            )
        ).scalar_one_or_none()
        if effect is None:
            raise CallbackCorrelationError("callback effect token is unknown")
        if effect.effect_type != _EXPECTED_EFFECT_TYPES[callback_kind]:
            raise CallbackCorrelationError("callback kind does not match the effect")

        deduplication_hash = _deduplication_hash(
            normalized,
            token_hash=token_hash,
        )
        receipt, created = _insert_or_get_receipt(
            session,
            effect=effect,
            normalized=normalized,
            token_hash=token_hash,
            payload_hash=payload_hash,
            deduplication_hash=deduplication_hash,
            received_at=received_at,
        )
        if apply_immediately and receipt.state in {
            ProviderCallbackState.PENDING,
            ProviderCallbackState.PROCESSING,
        }:
            applied_receipt = apply_callback_receipt(
                session,
                clinic_id=effect.clinic_id,
                receipt_id=receipt.id,
                now=received_at,
                skip_locked=True,
            )
            if applied_receipt is not None:
                receipt = applied_receipt
        session.flush()
        return CallbackReceiptResult(
            receipt_id=receipt.id,
            created=created,
            state=receipt.state,
            reason_code=receipt.reason_code,
        )


def normalize_twilio_callback(
    callback_kind: ProviderCallbackKind,
    fields: Mapping[str, str],
) -> NormalizedCallback:
    """Normalize only the provider fields needed by the callback contract."""
    _validate_callback_shape(fields)
    if callback_kind == ProviderCallbackKind.SMS:
        status = _required_status(fields, "MessageStatus", _SMS_GRAPH)
        resource_id = _optional_sid(fields, "MessageSid", _MESSAGE_SID_PATTERN)
        error_code = _optional_field(fields, "ErrorCode", max_length=10)
        if error_code and not error_code.isdigit():
            raise CallbackValidationError("invalid SMS error code")
        identity = (resource_id, status) if resource_id else None
        return NormalizedCallback(
            provider="twilio",
            callback_kind=callback_kind,
            provider_resource_id=resource_id,
            normalized_status=status,
            provider_sequence=None,
            provider_observed_at=_parse_sms_observed_at(fields),
            contract_identity=identity,
        )
    if callback_kind == ProviderCallbackKind.VOICE:
        status = _required_status(fields, "CallStatus", _VOICE_STATUSES)
        resource_id = _optional_sid(fields, "CallSid", _CALL_SID_PATTERN)
        sequence = _optional_sequence(fields, "SequenceNumber")
        identity = (resource_id, str(sequence)) if resource_id and sequence is not None else None
        return NormalizedCallback(
            provider="twilio",
            callback_kind=callback_kind,
            provider_resource_id=resource_id,
            normalized_status=status,
            provider_sequence=sequence,
            provider_observed_at=_parse_rfc2822(fields, "Timestamp"),
            contract_identity=identity,
        )
    if callback_kind == ProviderCallbackKind.RECORDING:
        status = _required_status(fields, "RecordingStatus", _RECORDING_GRAPH)
        resource_id = _optional_sid(fields, "RecordingSid", _RECORDING_SID_PATTERN)
        identity = (resource_id, status) if resource_id else None
        return NormalizedCallback(
            provider="twilio",
            callback_kind=callback_kind,
            provider_resource_id=resource_id,
            normalized_status=status,
            provider_sequence=None,
            provider_observed_at=_parse_rfc2822(fields, "RecordingStartTime"),
            contract_identity=identity,
        )
    if callback_kind == ProviderCallbackKind.AMD:
        status = _required_status(fields, "AnsweredBy", _AMD_STATUSES)
        resource_id = _optional_sid(fields, "CallSid", _CALL_SID_PATTERN)
        identity = (resource_id, status) if resource_id else None
        return NormalizedCallback(
            provider="twilio",
            callback_kind=callback_kind,
            provider_resource_id=resource_id,
            normalized_status=status,
            provider_sequence=None,
            provider_observed_at=None,
            contract_identity=identity,
        )
    raise CallbackValidationError("unsupported callback kind")


def claim_callback_receipts(
    session: Session,
    *,
    clinic_id: str,
    worker_id: str,
    now: datetime,
    lease_for: timedelta,
    limit: int = 50,
) -> list[str]:
    """Claim a finite batch of pending receipts with expiring row leases."""
    _require_aware(now, "now")
    if not clinic_id or not worker_id:
        raise ValueError("clinic_id and worker_id are required")
    if lease_for <= timedelta(0):
        raise ValueError("lease_for must be positive")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")

    with clinic_scope(session, clinic_id):
        session.execute(
            sa.update(ProviderCallbackReceipt)
            .where(
                ProviderCallbackReceipt.clinic_id == clinic_id,
                ProviderCallbackReceipt.state == ProviderCallbackState.PROCESSING,
                ProviderCallbackReceipt.lease_expires_at.is_not(None),
                ProviderCallbackReceipt.lease_expires_at <= now,
            )
            .values(
                state=ProviderCallbackState.PENDING,
                reason_code=ProviderCallbackReason.EFFECT_BUSY,
                lease_owner=None,
                lease_expires_at=None,
            )
        )
        statement = (
            tenant_select(ProviderCallbackReceipt)
            .where(ProviderCallbackReceipt.state == ProviderCallbackState.PENDING)
            .order_by(
                ProviderCallbackReceipt.received_at,
                ProviderCallbackReceipt.id,
            )
            .limit(limit)
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        receipts = list(session.execute(statement).scalars())
        lease_expires_at = now + lease_for
        for receipt in receipts:
            receipt.state = ProviderCallbackState.PROCESSING
            receipt.reason_code = None
            receipt.processing_attempts = int(receipt.processing_attempts or 0) + 1
            receipt.lease_owner = worker_id
            receipt.lease_expires_at = lease_expires_at
        session.flush()
        return [receipt.id for receipt in receipts]


def apply_callback_receipt(
    session: Session,
    *,
    clinic_id: str,
    receipt_id: str,
    now: datetime,
    worker_id: str | None = None,
    skip_locked: bool = False,
) -> ProviderCallbackReceipt | None:
    """Apply one receipt under row locks without invoking any provider."""
    _require_aware(now, "now")
    with clinic_scope(session, clinic_id):
        receipt_statement = tenant_select(ProviderCallbackReceipt).where(
            ProviderCallbackReceipt.id == receipt_id
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            receipt_statement = receipt_statement.with_for_update(skip_locked=skip_locked)
        receipt = session.execute(receipt_statement).scalar_one_or_none()
        if receipt is None:
            return None
        if receipt.state in {
            ProviderCallbackState.APPLIED,
            ProviderCallbackState.RECONCILE_REQUIRED,
        }:
            return receipt
        if receipt.state == ProviderCallbackState.PROCESSING:
            if not worker_id or receipt.lease_owner != worker_id:
                return None

        effect_statement = tenant_select(ExternalEffect).where(
            ExternalEffect.id == receipt.external_effect_id
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            effect_statement = effect_statement.with_for_update(skip_locked=skip_locked)
        effect = session.execute(effect_statement).scalar_one_or_none()
        if effect is None:
            receipt.reason_code = ProviderCallbackReason.EFFECT_BUSY
            session.flush()
            return receipt
        _apply_receipt(session, effect=effect, receipt=receipt, now=now)
        session.flush()
        return receipt


def reconcile_once(
    session_factory: Callable[[], Session],
    *,
    clinic_id: str,
    worker_id: str,
    now: datetime,
    enabled: bool = False,
    lease_for: timedelta = timedelta(minutes=5),
    limit: int = 50,
) -> ReconciliationResult:
    """Apply one finite receipt batch; never query or invoke a provider."""
    if not enabled:
        return ReconciliationResult(enabled=False)

    with session_factory() as session:
        receipt_ids = claim_callback_receipts(
            session,
            clinic_id=clinic_id,
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
            limit=limit,
        )
        session.commit()

    applied = 0
    conflicts = 0
    pending = 0
    for receipt_id in receipt_ids:
        with session_factory() as session:
            receipt = apply_callback_receipt(
                session,
                clinic_id=clinic_id,
                receipt_id=receipt_id,
                now=now,
                worker_id=worker_id,
                skip_locked=True,
            )
            if receipt is None or receipt.state in {
                ProviderCallbackState.PENDING,
                ProviderCallbackState.PROCESSING,
            }:
                pending += 1
            elif receipt.state == ProviderCallbackState.RECONCILE_REQUIRED:
                conflicts += 1
            else:
                applied += 1
            session.commit()

    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            unresolved_effects = int(
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(ExternalEffect)
                    .where(
                        ExternalEffect.clinic_id == clinic_id,
                        ExternalEffect.state == ExternalEffectState.RECONCILE_REQUIRED,
                    )
                )
                or 0
            )

    result = ReconciliationResult(
        enabled=True,
        claimed=len(receipt_ids),
        applied=applied,
        conflicts=conflicts,
        pending=pending,
        unresolved_effects=unresolved_effects,
    )
    emit_worker_summary("callback_reconcile", result.as_summary())
    return result


def _insert_or_get_receipt(
    session: Session,
    *,
    effect: ExternalEffect,
    normalized: NormalizedCallback,
    token_hash: str,
    payload_hash: str,
    deduplication_hash: str,
    received_at: datetime,
) -> tuple[ProviderCallbackReceipt, bool]:
    existing = _find_receipt(
        session,
        effect.clinic_id,
        normalized.provider,
        normalized.callback_kind,
        deduplication_hash,
    )
    if existing is not None:
        return _resolve_existing_receipt(
            session,
            effect=effect,
            existing=existing,
            normalized=normalized,
            received_at=received_at,
        )

    receipt = ProviderCallbackReceipt(
        id=f"receipt-{uuid.uuid4().hex}",
        clinic_id=effect.clinic_id,
        external_effect_id=effect.id,
        provider=normalized.provider,
        callback_kind=normalized.callback_kind,
        deduplication_hash=deduplication_hash,
        effect_token_hash=token_hash,
        provider_resource_id=normalized.provider_resource_id,
        normalized_status=normalized.normalized_status,
        provider_sequence=normalized.provider_sequence,
        provider_observed_at=normalized.provider_observed_at,
        payload_hash=payload_hash,
        state=ProviderCallbackState.PENDING,
        received_at=received_at,
    )
    if session.bind is None or session.bind.dialect.name != "postgresql":
        session.add(receipt)
        session.flush()
        return receipt, True

    savepoint = session.begin_nested()
    try:
        session.add(receipt)
        session.flush()
    except IntegrityError:
        savepoint.rollback()
        existing = _find_receipt(
            session,
            effect.clinic_id,
            normalized.provider,
            normalized.callback_kind,
            deduplication_hash,
        )
        if existing is None:
            raise
        return _resolve_existing_receipt(
            session,
            effect=effect,
            existing=existing,
            normalized=normalized,
            received_at=received_at,
        )
    else:
        savepoint.commit()
        return receipt, True


def _find_receipt(
    session: Session,
    clinic_id: str,
    provider: str,
    callback_kind: ProviderCallbackKind,
    deduplication_hash: str,
) -> ProviderCallbackReceipt | None:
    return session.execute(
        tenant_select(ProviderCallbackReceipt).where(
            ProviderCallbackReceipt.clinic_id == clinic_id,
            ProviderCallbackReceipt.provider == provider,
            ProviderCallbackReceipt.callback_kind == callback_kind,
            ProviderCallbackReceipt.deduplication_hash == deduplication_hash,
        )
    ).scalar_one_or_none()


def _resolve_existing_receipt(
    session: Session,
    *,
    effect: ExternalEffect,
    existing: ProviderCallbackReceipt,
    normalized: NormalizedCallback,
    received_at: datetime,
) -> tuple[ProviderCallbackReceipt, bool]:
    same_evidence = (
        existing.external_effect_id == effect.id
        and existing.provider_resource_id == normalized.provider_resource_id
        and existing.normalized_status == normalized.normalized_status
        and existing.provider_sequence == normalized.provider_sequence
    )
    if same_evidence:
        return existing, False

    reason = (
        ProviderCallbackReason.PROVIDER_IDENTITY_CONFLICT
        if existing.external_effect_id != effect.id
        else ProviderCallbackReason.CONFLICTING_TERMINAL
    )
    existing.state = ProviderCallbackState.RECONCILE_REQUIRED
    existing.reason_code = reason
    existing.applied_at = received_at
    _clear_receipt_lease(existing)
    _mark_effect_conflict(effect, reason)
    if existing.external_effect_id != effect.id:
        statement = tenant_select(ExternalEffect).where(
            ExternalEffect.id == existing.external_effect_id
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        prior_effect = session.execute(statement).scalar_one_or_none()
        if prior_effect is not None:
            _mark_effect_conflict(prior_effect, reason)
    session.flush()
    return existing, False


def _apply_receipt(
    session: Session,
    *,
    effect: ExternalEffect,
    receipt: ProviderCallbackReceipt,
    now: datetime,
) -> None:
    if (
        effect.state == ExternalEffectState.RECONCILE_REQUIRED
        and effect.last_error_class == "ProviderCallbackConflict"
    ):
        try:
            reason = ProviderCallbackReason(str(effect.last_error_code or ""))
        except ValueError:
            reason = ProviderCallbackReason.EFFECT_STATE_CONFLICT
        receipt.state = ProviderCallbackState.RECONCILE_REQUIRED
        receipt.reason_code = reason
        receipt.applied_at = now
        _clear_receipt_lease(receipt)
        _mark_recording_ledger_reconcile_required(session, effect)
        return
    if receipt.provider_resource_id is None or (
        receipt.callback_kind == ProviderCallbackKind.VOICE and receipt.provider_sequence is None
    ):
        receipt.state = ProviderCallbackState.RECONCILE_REQUIRED
        receipt.reason_code = ProviderCallbackReason.MISSING_EVIDENCE
        receipt.applied_at = now
        _clear_receipt_lease(receipt)
        return
    if effect.provider_resource_id and effect.provider_resource_id != receipt.provider_resource_id:
        _mark_recording_ledger_reconcile_required(session, effect)
        _quarantine(
            effect,
            receipt,
            ProviderCallbackReason.PROVIDER_IDENTITY_CONFLICT,
            now,
        )
        return
    if receipt.callback_kind == ProviderCallbackKind.AMD:
        prior_amd = session.execute(
            tenant_select(ProviderCallbackReceipt)
            .where(
                ProviderCallbackReceipt.external_effect_id == effect.id,
                ProviderCallbackReceipt.callback_kind == ProviderCallbackKind.AMD,
                ProviderCallbackReceipt.id != receipt.id,
            )
            .order_by(
                ProviderCallbackReceipt.received_at,
                ProviderCallbackReceipt.id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if (
            prior_amd is not None
            and prior_amd.normalized_status != receipt.normalized_status
        ):
            _quarantine(
                effect,
                receipt,
                ProviderCallbackReason.CONFLICTING_TERMINAL,
                now,
            )
            return
    if effect.state in {
        ExternalEffectState.PENDING,
        ExternalEffectState.LEASED,
        ExternalEffectState.REJECTED,
        ExternalEffectState.DEAD_LETTER,
        ExternalEffectState.CANCELED,
    }:
        _quarantine(
            effect,
            receipt,
            ProviderCallbackReason.EFFECT_STATE_CONFLICT,
            now,
        )
        return

    previous = _latest_applied_receipt(session, effect.id, receipt.callback_kind)
    ordering = _ordering_decision(effect, previous, receipt)
    if ordering == "stale":
        receipt.state = ProviderCallbackState.APPLIED
        receipt.reason_code = ProviderCallbackReason.STALE_NOOP
        receipt.applied_at = now
        _clear_receipt_lease(receipt)
        return
    if ordering == "conflict":
        _mark_recording_ledger_reconcile_required(session, effect)
        _quarantine(
            effect,
            receipt,
            ProviderCallbackReason.CONFLICTING_TERMINAL,
            now,
        )
        return

    category = _terminal_category(receipt.callback_kind, receipt.normalized_status)
    if receipt.callback_kind == ProviderCallbackKind.AMD and receipt.normalized_status == "unknown":
        receipt.state = ProviderCallbackState.RECONCILE_REQUIRED
        receipt.reason_code = ProviderCallbackReason.MISSING_EVIDENCE
        receipt.applied_at = now
        _clear_receipt_lease(receipt)
        effect.state = ExternalEffectState.RECONCILE_REQUIRED
        effect.provider_status = "provider_unresolved"
        effect.last_error_class = "ProviderCallbackUnresolved"
        effect.last_error_code = ProviderCallbackReason.MISSING_EVIDENCE.value
        effect.lease_owner = None
        effect.lease_expires_at = None
        return

    if receipt.callback_kind == ProviderCallbackKind.RECORDING and not _project_recording_ledger(
        session,
        effect=effect,
        receipt=receipt,
        now=now,
    ):
        return

    effect.provider_resource_id = receipt.provider_resource_id
    if receipt.callback_kind == ProviderCallbackKind.VOICE:
        effect.provider_sequence = receipt.provider_sequence
    preserve_unresolved_amd = (
        receipt.callback_kind == ProviderCallbackKind.VOICE
        and effect.provider_status == "provider_unresolved"
    )
    effect.state = (
        ExternalEffectState.RECONCILE_REQUIRED
        if preserve_unresolved_amd
        else ExternalEffectState.SUCCEEDED
    )
    neutral_status = _neutral_provider_status(
        receipt.callback_kind,
        receipt.normalized_status,
        category,
    )
    if not (
        receipt.callback_kind == ProviderCallbackKind.VOICE
        and effect.provider_status
        in {"human_confirmed", "non_human_confirmed", "provider_unresolved"}
    ):
        effect.provider_status = neutral_status
    effect.completion_evidence_hash = _completion_hash(effect)
    effect.completed_at = effect.completed_at or now
    effect.lease_owner = None
    effect.lease_expires_at = None
    if not preserve_unresolved_amd:
        effect.last_error_class = None
        effect.last_error_code = None
    _advance_outreach_job(session, effect, receipt)
    receipt.state = ProviderCallbackState.APPLIED
    receipt.reason_code = ProviderCallbackReason.APPLIED
    receipt.applied_at = now
    _clear_receipt_lease(receipt)


def _project_recording_ledger(
    session: Session,
    *,
    effect: ExternalEffect,
    receipt: ProviderCallbackReceipt,
    now: datetime,
) -> bool:
    if effect.aggregate_type != "call_record":
        return True
    expected_payload = {
        "intent": "recording_start",
        "call_record_id": effect.aggregate_id,
    }
    record = _recording_call_record(session, effect)
    if effect.payload_version != 1 or effect.payload != expected_payload or record is None:
        _quarantine(
            effect,
            receipt,
            ProviderCallbackReason.EFFECT_STATE_CONFLICT,
            now,
        )
        return False

    recording_sid = receipt.provider_resource_id
    assert recording_sid is not None  # nosec B101 - checked before projection
    if record.recording_sid is not None and record.recording_sid != recording_sid:
        record.recording_status = CallRecordingStatus.RECONCILE_REQUIRED
        _quarantine(
            effect,
            receipt,
            ProviderCallbackReason.PROVIDER_IDENTITY_CONFLICT,
            now,
        )
        return False

    record.recording_sid = recording_sid
    if receipt.normalized_status == "in-progress":
        terminal = record.recording_status in {
            CallRecordingStatus.COMPLETED,
            CallRecordingStatus.STORED,
            CallRecordingStatus.ABSENT,
        }
        stop_required = (
            record.recording_stop_requested_at is not None
            or record.consent_state == RecordingConsentState.WITHDRAWN
        )
        if not terminal:
            record.recording_status = CallRecordingStatus.IN_PROGRESS
            record.recording_started_at = (
                record.recording_started_at or receipt.provider_observed_at or now
            )
        if stop_required and not terminal:
            from ..recording import request_recording_stop

            request_recording_stop(
                session,
                clinic_id=record.clinic_id,
                call_record_id=record.id,
                now=now,
            )
    elif receipt.normalized_status == "completed":
        record.recording_status = CallRecordingStatus.COMPLETED
        record.recording_started_at = (
            record.recording_started_at or receipt.provider_observed_at or now
        )
        record.recording_stopped_at = record.recording_stopped_at or now
        _settle_recording_stop_effect(
            session,
            record=record,
            recording_sid=recording_sid,
            provider_status="recording_completed",
            now=now,
        )
    else:
        record.recording_status = CallRecordingStatus.ABSENT
        record.recording_stopped_at = record.recording_stopped_at or now
        _settle_recording_stop_effect(
            session,
            record=record,
            recording_sid=recording_sid,
            provider_status="recording_absent",
            now=now,
        )
    return True


def _settle_recording_stop_effect(
    session: Session,
    *,
    record: CallRecord,
    recording_sid: str,
    provider_status: str,
    now: datetime,
) -> None:
    statement = tenant_select(ExternalEffect).where(
        ExternalEffect.effect_type == ExternalEffectType.RECORDING,
        ExternalEffect.aggregate_type == "call_record",
        ExternalEffect.aggregate_id == record.id,
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    effects = list(session.execute(statement).scalars())
    stop_effects = [
        effect
        for effect in effects
        if effect.payload
        == {"intent": "recording_stop", "call_record_id": record.id}
    ]
    if len(stop_effects) != 1:
        return
    effect = stop_effects[0]
    if effect.provider_resource_id not in {None, recording_sid}:
        effect.state = ExternalEffectState.RECONCILE_REQUIRED
        effect.last_error_class = "ProviderCallbackConflict"
        effect.last_error_code = ProviderCallbackReason.PROVIDER_IDENTITY_CONFLICT.value
        effect.lease_owner = None
        effect.lease_expires_at = None
        record.recording_status = CallRecordingStatus.RECONCILE_REQUIRED
        return
    if effect.state in {
        ExternalEffectState.CANCELED,
        ExternalEffectState.REJECTED,
        ExternalEffectState.DEAD_LETTER,
    }:
        return
    effect.state = ExternalEffectState.SUCCEEDED
    effect.provider_resource_id = recording_sid
    effect.provider_status = provider_status
    effect.completion_evidence_hash = _completion_hash(effect)
    effect.completed_at = effect.completed_at or now
    effect.last_error_class = None
    effect.last_error_code = None
    effect.lease_owner = None
    effect.lease_expires_at = None


def _mark_recording_ledger_reconcile_required(
    session: Session,
    effect: ExternalEffect,
) -> None:
    record = _recording_call_record(session, effect)
    if record is not None:
        record.recording_status = CallRecordingStatus.RECONCILE_REQUIRED


def _recording_call_record(
    session: Session,
    effect: ExternalEffect,
) -> CallRecord | None:
    if effect.effect_type != ExternalEffectType.RECORDING or effect.aggregate_type != "call_record":
        return None
    statement = tenant_select(CallRecord).where(CallRecord.id == effect.aggregate_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    return session.execute(statement).scalar_one_or_none()


def _latest_applied_receipt(
    session: Session,
    effect_id: str,
    callback_kind: ProviderCallbackKind,
) -> ProviderCallbackReceipt | None:
    return session.execute(
        tenant_select(ProviderCallbackReceipt)
        .where(
            ProviderCallbackReceipt.external_effect_id == effect_id,
            ProviderCallbackReceipt.callback_kind == callback_kind,
            ProviderCallbackReceipt.state == ProviderCallbackState.APPLIED,
            ProviderCallbackReceipt.reason_code == ProviderCallbackReason.APPLIED,
        )
        .order_by(
            ProviderCallbackReceipt.applied_at.desc(),
            ProviderCallbackReceipt.id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def _ordering_decision(
    effect: ExternalEffect,
    previous: ProviderCallbackReceipt | None,
    incoming: ProviderCallbackReceipt,
) -> str:
    if previous is None:
        return "apply"
    previous_category = _terminal_category(
        previous.callback_kind,
        previous.normalized_status,
    )
    incoming_category = _terminal_category(
        incoming.callback_kind,
        incoming.normalized_status,
    )
    if previous_category and incoming_category and previous_category != incoming_category:
        return "conflict"

    if incoming.callback_kind == ProviderCallbackKind.VOICE:
        current_sequence = effect.provider_sequence
        assert incoming.provider_sequence is not None  # nosec B101
        if current_sequence is not None and incoming.provider_sequence <= current_sequence:
            return "stale"
        if previous_category and incoming_category is None:
            return "conflict"
        return "apply"
    if incoming.callback_kind == ProviderCallbackKind.SMS:
        return _graph_decision(
            _SMS_GRAPH,
            previous.normalized_status,
            incoming.normalized_status,
        )
    if incoming.callback_kind == ProviderCallbackKind.RECORDING:
        return _graph_decision(
            _RECORDING_GRAPH,
            previous.normalized_status,
            incoming.normalized_status,
        )
    if incoming.callback_kind == ProviderCallbackKind.AMD:
        return "stale" if previous.normalized_status == incoming.normalized_status else "conflict"
    return "conflict"


def _graph_decision(
    graph: dict[str, frozenset[str]],
    previous_status: str,
    incoming_status: str,
) -> str:
    if previous_status == incoming_status:
        return "stale"
    if _is_reachable(graph, previous_status, incoming_status):
        return "apply"
    if _is_reachable(graph, incoming_status, previous_status):
        return "stale"
    return "conflict"


def _is_reachable(
    graph: dict[str, frozenset[str]],
    start: str,
    target: str,
) -> bool:
    pending = list(graph.get(start, frozenset()))
    seen: set[str] = set()
    while pending:
        status = pending.pop()
        if status == target:
            return True
        if status not in seen:
            seen.add(status)
            pending.extend(graph.get(status, frozenset()))
    return False


def _terminal_category(
    callback_kind: ProviderCallbackKind,
    status: str,
) -> str | None:
    if status in _SUCCESS_STATUSES[callback_kind]:
        return "success"
    if status in _FAILURE_STATUSES[callback_kind]:
        return "failure"
    return None


def _neutral_provider_status(
    callback_kind: ProviderCallbackKind,
    status: str,
    category: str | None,
) -> str:
    if callback_kind == ProviderCallbackKind.SMS:
        if category == "success":
            return "delivery_succeeded"
        if category == "failure":
            return "delivery_failed"
        return "provider_observed"
    if callback_kind == ProviderCallbackKind.VOICE:
        if category == "success":
            return "call_completed"
        if category == "failure":
            return "call_failed"
        return "call_progress"
    if callback_kind == ProviderCallbackKind.RECORDING:
        if category == "success":
            return "recording_available"
        if category == "failure":
            return "recording_absent"
        return "recording_in_progress"
    if callback_kind == ProviderCallbackKind.AMD:
        return "human_confirmed" if category == "success" else "non_human_confirmed"
    raise CallbackValidationError("unsupported callback kind")


def _quarantine(
    effect: ExternalEffect,
    receipt: ProviderCallbackReceipt,
    reason: ProviderCallbackReason,
    now: datetime,
) -> None:
    receipt.state = ProviderCallbackState.RECONCILE_REQUIRED
    receipt.reason_code = reason
    receipt.applied_at = now
    _clear_receipt_lease(receipt)
    _mark_effect_conflict(effect, reason)


def _mark_effect_conflict(
    effect: ExternalEffect,
    reason: ProviderCallbackReason,
) -> None:
    effect.state = ExternalEffectState.RECONCILE_REQUIRED
    effect.last_error_class = "ProviderCallbackConflict"
    effect.last_error_code = reason.value
    effect.lease_owner = None
    effect.lease_expires_at = None


def _clear_receipt_lease(receipt: ProviderCallbackReceipt) -> None:
    receipt.lease_owner = None
    receipt.lease_expires_at = None


def _advance_outreach_job(
    session: Session,
    effect: ExternalEffect,
    receipt: ProviderCallbackReceipt,
) -> None:
    if (
        effect.aggregate_type != "outreach_job"
        or receipt.callback_kind != ProviderCallbackKind.SMS
    ):
        return
    statement = tenant_select(OutreachJob).where(OutreachJob.id == effect.aggregate_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    job = session.execute(statement).scalar_one_or_none()
    if job is None or job.state in {
        OutreachState.DELIVERED,
        OutreachState.REPLIED,
        OutreachState.ESCALATED,
        OutreachState.COMPLETED,
    }:
        return
    category = _terminal_category(receipt.callback_kind, receipt.normalized_status)
    if category == "success":
        job.state = (
            OutreachState.DELIVERED
            if receipt.callback_kind == ProviderCallbackKind.SMS
            else OutreachState.COMPLETED
        )
        job.next_action_at = None
    elif category == "failure":
        job.state = OutreachState.FAILED
        job.next_action_at = None
    elif job.state == OutreachState.QUEUED:
        job.state = OutreachState.SENT


def _completion_hash(effect: ExternalEffect) -> str:
    encoded = json.dumps(
        {
            "provider_resource_id": effect.provider_resource_id,
            "request_hash": effect.request_hash,
            "status": effect.provider_status,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _deduplication_hash(
    normalized: NormalizedCallback,
    *,
    token_hash: str,
) -> str:
    if normalized.contract_identity is not None:
        identity = list(normalized.contract_identity)
    else:
        identity = [
            token_hash,
            normalized.normalized_status,
            str(normalized.provider_sequence) if normalized.provider_sequence is not None else "",
        ]
    encoded = json.dumps(
        {
            "callback_kind": normalized.callback_kind.value,
            "identity": identity,
            "provider": normalized.provider,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_status(
    fields: Mapping[str, str],
    field_name: str,
    allowed: Mapping[str, object] | frozenset[str],
) -> str:
    value = _optional_field(fields, field_name, max_length=32)
    if not value or value not in allowed:
        raise CallbackValidationError("callback status is missing or unsupported")
    return value.lower()


def _validate_callback_shape(fields: Mapping[str, str]) -> None:
    if len(fields) > _MAX_CALLBACK_FIELDS:
        raise CallbackValidationError("callback contains too many fields")
    for name, value in fields.items():
        if not name or len(str(name)) > _MAX_CALLBACK_FIELD_NAME:
            raise CallbackValidationError("callback field name exceeds the supported size")
        if len(str(value)) > _MAX_CALLBACK_FIELD_VALUE:
            raise CallbackValidationError("callback field exceeds the supported size")


def _optional_sid(
    fields: Mapping[str, str],
    field_name: str,
    pattern: re.Pattern[str],
) -> str | None:
    value = _optional_field(fields, field_name, max_length=128)
    if not value:
        return None
    if not pattern.fullmatch(value):
        raise CallbackValidationError("provider resource identity is malformed")
    return value


def _optional_sequence(fields: Mapping[str, str], field_name: str) -> int | None:
    value = _optional_field(fields, field_name, max_length=10)
    if not value:
        return None
    if not value.isdigit():
        raise CallbackValidationError("provider sequence is malformed")
    sequence = int(value)
    if sequence > 2_147_483_647:
        raise CallbackValidationError("provider sequence is out of range")
    return sequence


def _optional_field(
    fields: Mapping[str, str],
    field_name: str,
    *,
    max_length: int,
) -> str:
    value = str(fields.get(field_name) or "").strip()
    if len(value) > max_length:
        raise CallbackValidationError("callback field exceeds the supported size")
    return value


def _parse_rfc2822(
    fields: Mapping[str, str],
    field_name: str,
) -> datetime | None:
    value = _optional_field(fields, field_name, max_length=64)
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise CallbackValidationError("provider observation time is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CallbackValidationError("provider observation time lacks a timezone")
    return parsed.astimezone(UTC)


def _parse_sms_observed_at(fields: Mapping[str, str]) -> datetime | None:
    value = _optional_field(fields, "RawDlrDoneDate", max_length=10)
    if not value:
        return None
    if not re.fullmatch(r"[0-9]{10}", value):
        raise CallbackValidationError("SMS observation time is malformed")
    try:
        return datetime.strptime(value, "%y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError as exc:
        raise CallbackValidationError("SMS observation time is malformed") from exc


def _validate_clinic_id(clinic_id: str) -> None:
    if not _CLINIC_ID_PATTERN.fullmatch(clinic_id):
        raise EffectTokenError("invalid clinic scope for effect token")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CallbackValidationError(f"{name} must be timezone-aware")
