"""Minimized application of authenticated ACS Email delivery reports."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import clinic_scope, tenant_select
from ..enums import (
    ExternalEffectType,
    HandoffDeliveryState,
    ProviderCallbackKind,
    ProviderCallbackReason,
    ProviderCallbackState,
)
from ..handoffs import (
    pause_clinic_programmes_for_handoff,
    request_alternate_notification,
)
from ..models import Clinic, ExternalEffect, HandoffReceipt, ProviderCallbackReceipt

_EVENT_TYPE = "Microsoft.Communication.EmailDeliveryReportReceived"
_DELIVERED = frozenset({"delivered"})
_TERMINAL_FAILURE = frozenset(
    {"bounced", "failed", "filteredspam", "quarantined", "suppressed"}
)
_NONTERMINAL = frozenset({"expanded"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MESSAGE_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\Z"
)
_MAX_EVENTS = 50
_MAX_PAYLOAD_BYTES = 256_000
_MAX_CLINICS = 1_000


class HandoffDeliveryValidationError(ValueError):
    """Authenticated payload failed the closed ACS Email event contract."""


class HandoffDeliveryCorrelationError(LookupError):
    """Authenticated payload did not bind to one handoff notification effect."""


@dataclass(frozen=True)
class HandoffDeliveryResult:
    """Aggregate-only result of one authenticated Event Grid request."""

    received: int
    created: int
    delivered: int
    definitive_failures: int
    nonterminal: int
    alternate_requested: int
    programmes_paused: int


def receive_acs_email_events(
    session: Session,
    *,
    events: Sequence[object],
    raw_payload: bytes,
    received_at: datetime,
) -> HandoffDeliveryResult:
    """Persist hashes and apply already-authenticated Email delivery reports."""
    _require_aware(received_at)
    if not 1 <= len(events) <= _MAX_EVENTS:
        raise HandoffDeliveryValidationError("Email delivery event count is invalid")
    if not 1 <= len(raw_payload) <= _MAX_PAYLOAD_BYTES:
        raise HandoffDeliveryValidationError("Email delivery payload size is invalid")
    received_at = received_at.astimezone(UTC)
    created_count = 0
    delivered = 0
    failures = 0
    nonterminal = 0
    alternate = 0
    paused = 0
    for raw_event in events:
        event = _parse_event(raw_event)
        clinic_id, effect = _resolve_effect(session, event.message_id)
        with clinic_scope(session, clinic_id):
            receipt = session.execute(
                tenant_select(HandoffReceipt).where(
                    HandoffReceipt.id == effect.aggregate_id
                )
            ).scalar_one_or_none()
            if receipt is None or effect.aggregate_type != "handoff_receipt":
                raise HandoffDeliveryCorrelationError(
                    "Email delivery effect has no handoff receipt"
                )
            callback, created = _insert_or_get_callback(
                session,
                effect=effect,
                event=event,
                payload_hash=hashlib.sha256(raw_payload).hexdigest(),
                received_at=received_at,
            )
            created_count += int(created)
            if callback.state == ProviderCallbackState.RECONCILE_REQUIRED:
                continue
            if event.status in _DELIVERED:
                if receipt.delivery_state != HandoffDeliveryState.DELIVERED:
                    receipt.delivery_state = HandoffDeliveryState.DELIVERED
                    receipt.delivered_at = receipt.delivered_at or received_at
                    delivered += 1
                callback.reason_code = ProviderCallbackReason.APPLIED
            elif event.status in _TERMINAL_FAILURE:
                if receipt.delivery_state != HandoffDeliveryState.DELIVERED:
                    receipt.delivery_state = HandoffDeliveryState.DEFINITIVE_FAILURE
                    failures += 1
                    alternate += int(
                        request_alternate_notification(
                            session,
                            receipt,
                            now=received_at,
                            reason_code="provider_terminal_nondelivery",
                        )
                    )
                    paused += pause_clinic_programmes_for_handoff(
                        session,
                        clinic_id=clinic_id,
                        now=received_at,
                        reason_code="handoff_destination_unavailable",
                    )
                callback.reason_code = ProviderCallbackReason.APPLIED
            else:
                nonterminal += 1
                callback.reason_code = ProviderCallbackReason.STALE_NOOP
            callback.state = ProviderCallbackState.APPLIED
            callback.applied_at = received_at
            callback.lease_owner = None
            callback.lease_expires_at = None
            session.flush()
    return HandoffDeliveryResult(
        received=len(events),
        created=created_count,
        delivered=delivered,
        definitive_failures=failures,
        nonterminal=nonterminal,
        alternate_requested=alternate,
        programmes_paused=paused,
    )


def _resolve_effect(
    session: Session,
    message_id: str,
) -> tuple[str, ExternalEffect]:
    clinic_ids = list(
        session.execute(
            sa.select(Clinic.id).order_by(Clinic.id).limit(_MAX_CLINICS + 1)
        ).scalars()
    )
    if len(clinic_ids) > _MAX_CLINICS:
        raise HandoffDeliveryCorrelationError(
            "Email delivery clinic scope exceeds the supported bound"
        )
    matches: list[tuple[str, ExternalEffect]] = []
    for clinic_id in clinic_ids:
        with clinic_scope(session, clinic_id):
            effect = session.execute(
                tenant_select(ExternalEffect).where(
                    ExternalEffect.effect_type
                    == ExternalEffectType.HANDOFF_NOTIFICATION,
                    ExternalEffect.provider_resource_id == message_id,
                )
            ).scalar_one_or_none()
            if effect is not None:
                matches.append((clinic_id, effect))
    if len(matches) != 1:
        raise HandoffDeliveryCorrelationError(
            "Email delivery message id is unknown or ambiguous"
        )
    return matches[0]


@dataclass(frozen=True)
class _EmailEvent:
    event_id: str
    message_id: str
    status: str
    event_time: datetime


def _parse_event(raw: object) -> _EmailEvent:
    if not isinstance(raw, Mapping):
        raise HandoffDeliveryValidationError("Email delivery event must be an object")
    event_type = str(raw.get("eventType") or "")
    if event_type != _EVENT_TYPE:
        raise HandoffDeliveryValidationError("unsupported Email delivery event type")
    event_id = str(raw.get("id") or "")
    data = raw.get("data")
    if _ID.fullmatch(event_id) is None or not isinstance(data, Mapping):
        raise HandoffDeliveryValidationError("Email delivery event identity is invalid")
    message_id = str(data.get("messageId") or "")
    if _MESSAGE_ID.fullmatch(message_id) is None:
        raise HandoffDeliveryValidationError("Email delivery message id is invalid")
    status = str(data.get("status") or "").strip().lower()
    if status not in _DELIVERED | _TERMINAL_FAILURE | _NONTERMINAL:
        raise HandoffDeliveryValidationError("Email delivery status is unsupported")
    event_time = _parse_time(raw.get("eventTime"))
    return _EmailEvent(
        event_id=event_id,
        message_id=message_id,
        status=status,
        event_time=event_time,
    )


def _insert_or_get_callback(
    session: Session,
    *,
    effect: ExternalEffect,
    event: _EmailEvent,
    payload_hash: str,
    received_at: datetime,
) -> tuple[ProviderCallbackReceipt, bool]:
    deduplication_hash = hashlib.sha256(
        f"acs-email:{event.event_id}:{event.message_id}".encode()
    ).hexdigest()
    existing = session.execute(
        tenant_select(ProviderCallbackReceipt).where(
            ProviderCallbackReceipt.provider == "acs_email",
            ProviderCallbackReceipt.callback_kind == ProviderCallbackKind.EMAIL,
            ProviderCallbackReceipt.deduplication_hash == deduplication_hash,
        )
    ).scalar_one_or_none()
    if existing is not None:
        same = (
            existing.external_effect_id == effect.id
            and existing.provider_resource_id == event.message_id
            and existing.normalized_status == event.status
        )
        if not same:
            existing.state = ProviderCallbackState.RECONCILE_REQUIRED
            existing.reason_code = ProviderCallbackReason.CONFLICTING_TERMINAL
        return existing, False
    callback = ProviderCallbackReceipt(
        id=f"receipt-{uuid.uuid4().hex}",
        clinic_id=effect.clinic_id,
        external_effect_id=effect.id,
        provider="acs_email",
        callback_kind=ProviderCallbackKind.EMAIL,
        deduplication_hash=deduplication_hash,
        effect_token_hash=hashlib.sha256(
            effect.callback_token.encode("utf-8")
        ).hexdigest(),
        provider_resource_id=event.message_id,
        normalized_status=event.status,
        provider_sequence=None,
        provider_observed_at=event.event_time,
        payload_hash=payload_hash,
        state=ProviderCallbackState.PENDING,
        reason_code=None,
        processing_attempts=0,
        received_at=received_at,
    )
    savepoint = session.begin_nested()
    try:
        session.add(callback)
        session.flush()
    except IntegrityError:
        savepoint.rollback()
        existing = session.execute(
            tenant_select(ProviderCallbackReceipt).where(
                ProviderCallbackReceipt.provider == "acs_email",
                ProviderCallbackReceipt.callback_kind == ProviderCallbackKind.EMAIL,
                ProviderCallbackReceipt.deduplication_hash == deduplication_hash,
            )
        ).scalar_one()
        return existing, False
    savepoint.commit()
    return callback, True


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise HandoffDeliveryValidationError("Email delivery event time is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HandoffDeliveryValidationError(
            "Email delivery event time is invalid"
        ) from exc
    _require_aware(parsed)
    return parsed.astimezone(UTC)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("received_at must be timezone-aware")