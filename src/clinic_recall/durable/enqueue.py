"""Transactional enqueue helpers for minimized external effects."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import clinic_scope, tenant_select
from ..enums import (
    BookingActionType,
    BookingWriteBackState,
    ExternalEffectState,
    ExternalEffectType,
    HandoffDestinationRole,
    HandoffRouteKind,
)
from ..models import (
    BookingAction,
    CallRecord,
    ExternalEffect,
    HandoffReceipt,
    OutreachJob,
)
from .callbacks import generate_effect_token

_OUTREACH_AGGREGATE_TYPE = "outreach_job"
_CALL_RECORD_AGGREGATE_TYPE = "call_record"
_RIGHTS_TARGET_AGGREGATE_TYPE = "rights_target"
_BOOKING_ACTION_AGGREGATE_TYPE = "booking_action"
_HANDOFF_RECEIPT_AGGREGATE_TYPE = "handoff_receipt"
_SMS_INTENT = "recall"
_CALL_INTENT = "recall_fallback"
_RECORDING_START_INTENT = "recording_start"
_RECORDING_STOP_INTENT = "recording_stop"
_RIGHTS_TARGET_INTENT = "rights_target_execute"
_BOOKING_CONFIRMATION_INTENT = "booking_confirmation"
_HANDOFF_TEMPLATE_VERSION = "handoff-v1"
_PAYLOAD_VERSION = 1


def handoff_notification_effect_identity(
    *,
    receipt_id: str,
    destination_role: HandoffDestinationRole | str,
    route_kind: HandoffRouteKind | str,
    severity_generation: int,
    template_version: str = _HANDOFF_TEMPLATE_VERSION,
) -> tuple[dict[str, object], str, str]:
    """Return one closed, address-free operational notification identity."""
    if not receipt_id:
        raise ValueError("handoff receipt id is required")
    role = HandoffDestinationRole(destination_role)
    route = HandoffRouteKind(route_kind)
    if not 0 <= severity_generation <= 16:
        raise ValueError("handoff severity generation is out of bounds")
    if template_version != _HANDOFF_TEMPLATE_VERSION:
        raise ValueError("unsupported handoff notification template")
    payload: dict[str, object] = {
        "destination_role": role.value,
        "receipt_id": receipt_id,
        "route_kind": route.value,
        "template_version": template_version,
    }
    idempotency_key = (
        f"handoff-notification:{receipt_id}:{role.value}:"
        f"{severity_generation}:v1"
    )
    request_hash = _request_hash(
        aggregate_type=_HANDOFF_RECEIPT_AGGREGATE_TYPE,
        aggregate_id=receipt_id,
        effect_type=ExternalEffectType.HANDOFF_NOTIFICATION,
        payload_version=_PAYLOAD_VERSION,
        payload=payload,
    )
    return payload, idempotency_key, request_hash


def enqueue_handoff_notification_effect(
    session: Session,
    *,
    clinic_id: str,
    receipt_id: str,
    severity_generation: int,
    available_at: datetime,
) -> tuple[ExternalEffect, bool]:
    """Enqueue one non-replaying primary operational notification."""
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise ValueError("available_at must be timezone-aware")
    available_at = available_at.astimezone(UTC)
    payload, idempotency_key, request_hash = handoff_notification_effect_identity(
        receipt_id=receipt_id,
        destination_role=HandoffDestinationRole.CLINIC_OPERATIONS,
        route_kind=HandoffRouteKind.OPERATIONAL_EMAIL,
        severity_generation=severity_generation,
    )
    with clinic_scope(session, clinic_id):
        receipt = session.execute(
            tenant_select(HandoffReceipt).where(HandoffReceipt.id == receipt_id)
        ).scalar_one_or_none()
        if receipt is None:
            raise LookupError("handoff receipt not found for clinic")
        if receipt.severity_generation != severity_generation:
            raise ValueError("handoff severity generation is stale")
        existing = _find_existing(
            session,
            ExternalEffectType.HANDOFF_NOTIFICATION,
            idempotency_key,
        )
        if existing is not None:
            _ensure_same_request(existing, request_hash)
            return existing, False
        effect = ExternalEffect(
            id=f"effect-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            aggregate_type=_HANDOFF_RECEIPT_AGGREGATE_TYPE,
            aggregate_id=receipt.id,
            effect_type=ExternalEffectType.HANDOFF_NOTIFICATION,
            idempotency_key=idempotency_key,
            callback_token=generate_effect_token(clinic_id),
            payload_version=_PAYLOAD_VERSION,
            payload=payload,
            request_hash=request_hash,
            state=ExternalEffectState.PENDING,
            available_at=available_at,
            max_attempts=1,
        )
        savepoint = session.begin_nested()
        try:
            session.add(effect)
            session.flush()
        except IntegrityError:
            savepoint.rollback()
            existing = _find_existing(
                session,
                ExternalEffectType.HANDOFF_NOTIFICATION,
                idempotency_key,
            )
            if existing is None:
                raise
            _ensure_same_request(existing, request_hash)
            return existing, False
        savepoint.commit()
        return effect, True


def cliniko_booking_effect_identity(
    *,
    booking_action_id: str,
    booking_request_hash: str,
) -> tuple[dict[str, object], str, str]:
    """Return the canonical closed payload, logical key, and request hash."""
    if not booking_action_id or len(booking_request_hash) != 64:
        raise ValueError("booking action request identity is invalid")
    payload: dict[str, object] = {
        "intent": "create",
        "booking_action_id": booking_action_id,
    }
    idempotency_key = (
        f"cliniko-booking:{booking_action_id}:{booking_request_hash}:v1"
    )
    request_hash = _request_hash(
        aggregate_type=_BOOKING_ACTION_AGGREGATE_TYPE,
        aggregate_id=booking_action_id,
        effect_type=ExternalEffectType.CLINIKO_BOOKING,
        payload_version=_PAYLOAD_VERSION,
        payload=payload,
    )
    return payload, idempotency_key, request_hash


def booking_confirmation_effect_identity(
    *,
    outreach_job_id: str,
    booking_action_id: str,
    completion_evidence_hash: str,
) -> tuple[dict[str, object], str, str]:
    """Return the canonical verified-confirmation payload and request identity."""
    if not outreach_job_id or not booking_action_id:
        raise ValueError("booking confirmation references are required")
    if re.fullmatch(r"[0-9a-f]{64}", completion_evidence_hash) is None:
        raise ValueError("completion evidence hash is invalid")
    payload: dict[str, object] = {
        "intent": _BOOKING_CONFIRMATION_INTENT,
        "outreach_job_id": outreach_job_id,
        "booking_action_id": booking_action_id,
    }
    idempotency_key = (
        f"booking-confirmation:{booking_action_id}:{completion_evidence_hash}:v1"
    )
    request_hash = _request_hash(
        aggregate_type=_OUTREACH_AGGREGATE_TYPE,
        aggregate_id=outreach_job_id,
        effect_type=ExternalEffectType.SMS,
        payload_version=_PAYLOAD_VERSION,
        payload=payload,
    )
    return payload, idempotency_key, request_hash


def enqueue_cliniko_booking_effect(
    session: Session,
    *,
    clinic_id: str,
    booking_action_id: str,
    intent: str,
    available_at: datetime,
    max_attempts: int = 2,
) -> tuple[ExternalEffect, bool]:
    """Enqueue one create-only Cliniko intent in the caller's transaction."""
    if intent != "create":
        raise ValueError("cliniko_reschedule_unavailable")
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise ValueError("available_at must be timezone-aware")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    available_at = available_at.astimezone(UTC)
    with clinic_scope(session, clinic_id):
        action = session.execute(
            tenant_select(BookingAction).where(BookingAction.id == booking_action_id)
        ).scalar_one_or_none()
        if action is None:
            raise LookupError("booking action not found for clinic")
        if action.type != BookingActionType.BOOK:
            raise ValueError("cliniko_reschedule_unavailable")
        if not action.request_hash:
            raise ValueError("booking action request identity is missing")
        payload, idempotency_key, request_hash = cliniko_booking_effect_identity(
            booking_action_id=action.id,
            booking_request_hash=action.request_hash,
        )
        existing = _find_existing(
            session,
            ExternalEffectType.CLINIKO_BOOKING,
            idempotency_key,
        )
        if existing is not None:
            _ensure_same_request(existing, request_hash)
            return existing, False
        effect = ExternalEffect(
            id=f"effect-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            aggregate_type=_BOOKING_ACTION_AGGREGATE_TYPE,
            aggregate_id=action.id,
            effect_type=ExternalEffectType.CLINIKO_BOOKING,
            idempotency_key=idempotency_key,
            callback_token=generate_effect_token(clinic_id),
            payload_version=_PAYLOAD_VERSION,
            payload=payload,
            request_hash=request_hash,
            state=ExternalEffectState.PENDING,
            available_at=available_at,
            max_attempts=max_attempts,
        )
        action.write_back_state = BookingWriteBackState.PENDING
        session.add(effect)
        session.flush()
        return effect, True


def enqueue_booking_confirmation_effect(
    session: Session,
    *,
    clinic_id: str,
    outreach_job_id: str,
    booking_action_id: str,
    completion_evidence_hash: str,
    available_at: datetime,
) -> tuple[ExternalEffect, bool]:
    """Release one SMS confirmation only for immutable verified evidence."""
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise ValueError("available_at must be timezone-aware")
    available_at = available_at.astimezone(UTC)
    payload, idempotency_key, request_hash = booking_confirmation_effect_identity(
        outreach_job_id=outreach_job_id,
        booking_action_id=booking_action_id,
        completion_evidence_hash=completion_evidence_hash,
    )
    with clinic_scope(session, clinic_id):
        job = session.execute(
            tenant_select(OutreachJob).where(OutreachJob.id == outreach_job_id)
        ).scalar_one_or_none()
        if job is None:
            raise LookupError("outreach job not found for clinic")
        existing = _find_existing(session, ExternalEffectType.SMS, idempotency_key)
        if existing is not None:
            _ensure_same_request(existing, request_hash)
            return existing, False
        effect = ExternalEffect(
            id=f"effect-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            aggregate_type=_OUTREACH_AGGREGATE_TYPE,
            aggregate_id=outreach_job_id,
            effect_type=ExternalEffectType.SMS,
            idempotency_key=idempotency_key,
            callback_token=generate_effect_token(clinic_id),
            payload_version=_PAYLOAD_VERSION,
            payload=payload,
            request_hash=request_hash,
            state=ExternalEffectState.PENDING,
            available_at=available_at,
            max_attempts=3,
        )
        session.add(effect)
        session.flush()
        return effect, True


def enqueue_sms_effect(
    session: Session,
    *,
    clinic_id: str,
    outreach_job_id: str,
    idempotency_key: str,
    available_at: datetime,
    max_attempts: int = 3,
) -> tuple[ExternalEffect, bool]:
    """Enqueue one logical SMS effect in the caller's transaction."""
    return _enqueue_outreach_effect(
        session,
        clinic_id=clinic_id,
        outreach_job_id=outreach_job_id,
        effect_type=ExternalEffectType.SMS,
        intent=_SMS_INTENT,
        idempotency_key=idempotency_key,
        available_at=available_at,
        max_attempts=max_attempts,
    )


def enqueue_call_effect(
    session: Session,
    *,
    clinic_id: str,
    outreach_job_id: str,
    idempotency_key: str,
    available_at: datetime,
    max_attempts: int = 1,
) -> tuple[ExternalEffect, bool]:
    """Enqueue one logical voice-fallback request without provider data."""
    return _enqueue_outreach_effect(
        session,
        clinic_id=clinic_id,
        outreach_job_id=outreach_job_id,
        effect_type=ExternalEffectType.CALL,
        intent=_CALL_INTENT,
        idempotency_key=idempotency_key,
        available_at=available_at,
        max_attempts=max_attempts,
    )


def enqueue_recording_start_effect(
    session: Session,
    *,
    clinic_id: str,
    call_record_id: str,
    available_at: datetime,
) -> tuple[ExternalEffect, bool]:
    """Enqueue one non-retrying provider recording-start request."""
    return _enqueue_recording_effect(
        session,
        clinic_id=clinic_id,
        call_record_id=call_record_id,
        intent=_RECORDING_START_INTENT,
        idempotency_key=f"recording:{call_record_id}:start:v1",
        available_at=available_at,
    )


def enqueue_recording_stop_effect(
    session: Session,
    *,
    clinic_id: str,
    call_record_id: str,
    available_at: datetime,
) -> tuple[ExternalEffect, bool]:
    """Enqueue one non-retrying provider recording-stop request."""
    return _enqueue_recording_effect(
        session,
        clinic_id=clinic_id,
        call_record_id=call_record_id,
        intent=_RECORDING_STOP_INTENT,
        idempotency_key=f"recording:{call_record_id}:stop:v1",
        available_at=available_at,
    )


def enqueue_rights_effect(
    session: Session,
    *,
    clinic_id: str,
    target_id: str,
    attempt_ordinal: int,
    available_at: datetime,
) -> tuple[ExternalEffect, bool]:
    """Enqueue one minimized, non-replaying rights target attempt."""
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise ValueError("available_at must be timezone-aware")
    if not clinic_id or not target_id:
        raise ValueError("clinic_id and target_id are required")
    if attempt_ordinal < 1:
        raise ValueError("attempt_ordinal must be at least 1")
    available_at = available_at.astimezone(UTC)
    payload: dict[str, object] = {
        "intent": _RIGHTS_TARGET_INTENT,
        "target_id": target_id,
        "attempt_ordinal": attempt_ordinal,
    }
    idempotency_key = f"rights:{target_id}:attempt:{attempt_ordinal}"
    request_hash = _request_hash(
        aggregate_type=_RIGHTS_TARGET_AGGREGATE_TYPE,
        aggregate_id=target_id,
        effect_type=ExternalEffectType.RIGHTS,
        payload_version=_PAYLOAD_VERSION,
        payload=payload,
    )
    with clinic_scope(session, clinic_id):
        existing = _find_existing(
            session,
            ExternalEffectType.RIGHTS,
            idempotency_key,
        )
        if existing is not None:
            _ensure_same_request(existing, request_hash)
            return existing, False
        effect = ExternalEffect(
            id=f"effect-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            aggregate_type=_RIGHTS_TARGET_AGGREGATE_TYPE,
            aggregate_id=target_id,
            effect_type=ExternalEffectType.RIGHTS,
            idempotency_key=idempotency_key,
            callback_token=generate_effect_token(clinic_id),
            payload_version=_PAYLOAD_VERSION,
            payload=payload,
            request_hash=request_hash,
            state=ExternalEffectState.PENDING,
            available_at=available_at,
            max_attempts=1,
        )
        if session.bind is None or session.bind.dialect.name != "postgresql":
            session.add(effect)
            session.flush()
            return effect, True
        savepoint = session.begin_nested()
        try:
            session.add(effect)
            session.flush()
        except IntegrityError:
            savepoint.rollback()
            existing = _find_existing(
                session,
                ExternalEffectType.RIGHTS,
                idempotency_key,
            )
            if existing is None:
                raise
            _ensure_same_request(existing, request_hash)
            return existing, False
        else:
            savepoint.commit()
            return effect, True


def _enqueue_recording_effect(
    session: Session,
    *,
    clinic_id: str,
    call_record_id: str,
    intent: str,
    idempotency_key: str,
    available_at: datetime,
) -> tuple[ExternalEffect, bool]:
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise ValueError("available_at must be timezone-aware")
    if not clinic_id or not call_record_id:
        raise ValueError("clinic_id and call_record_id are required")
    available_at = available_at.astimezone(UTC)
    payload = {"intent": intent, "call_record_id": call_record_id}
    request_hash = _request_hash(
        aggregate_type=_CALL_RECORD_AGGREGATE_TYPE,
        aggregate_id=call_record_id,
        effect_type=ExternalEffectType.RECORDING,
        payload_version=_PAYLOAD_VERSION,
        payload=payload,
    )
    with clinic_scope(session, clinic_id):
        record = session.execute(
            tenant_select(CallRecord).where(CallRecord.id == call_record_id)
        ).scalar_one_or_none()
        if record is None:
            raise LookupError(f"call record {call_record_id!r} not found for clinic")
        if intent == _RECORDING_START_INTENT and record.patient_id:
            from ..rights import assert_patient_writable

            assert_patient_writable(session, clinic_id, record.patient_id)
        existing = _find_existing(
            session,
            ExternalEffectType.RECORDING,
            idempotency_key,
        )
        if existing is not None:
            _ensure_same_request(existing, request_hash)
            return existing, False
        effect = ExternalEffect(
            id=f"effect-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            aggregate_type=_CALL_RECORD_AGGREGATE_TYPE,
            aggregate_id=call_record_id,
            effect_type=ExternalEffectType.RECORDING,
            idempotency_key=idempotency_key,
            callback_token=generate_effect_token(clinic_id),
            payload_version=_PAYLOAD_VERSION,
            payload=payload,
            request_hash=request_hash,
            state=ExternalEffectState.PENDING,
            available_at=available_at,
            max_attempts=1,
        )
        if session.bind is None or session.bind.dialect.name != "postgresql":
            session.add(effect)
            session.flush()
            return effect, True
        savepoint = session.begin_nested()
        try:
            session.add(effect)
            session.flush()
        except IntegrityError:
            savepoint.rollback()
            existing = _find_existing(
                session,
                ExternalEffectType.RECORDING,
                idempotency_key,
            )
            if existing is None:
                raise
            _ensure_same_request(existing, request_hash)
            return existing, False
        else:
            savepoint.commit()
            return effect, True


def _enqueue_outreach_effect(
    session: Session,
    *,
    clinic_id: str,
    outreach_job_id: str,
    effect_type: ExternalEffectType,
    intent: str,
    idempotency_key: str,
    available_at: datetime,
    max_attempts: int,
) -> tuple[ExternalEffect, bool]:
    if available_at.tzinfo is None or available_at.utcoffset() is None:
        raise ValueError("available_at must be timezone-aware")
    available_at = available_at.astimezone(UTC)
    if not clinic_id or not outreach_job_id or not idempotency_key:
        raise ValueError("clinic_id, outreach_job_id, and idempotency_key are required")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    payload = {
        "intent": intent,
        "outreach_job_id": outreach_job_id,
    }
    request_hash = _request_hash(
        aggregate_type=_OUTREACH_AGGREGATE_TYPE,
        aggregate_id=outreach_job_id,
        effect_type=effect_type,
        payload_version=_PAYLOAD_VERSION,
        payload=payload,
    )

    with clinic_scope(session, clinic_id):
        job = session.execute(
            tenant_select(OutreachJob).where(OutreachJob.id == outreach_job_id)
        ).scalar_one_or_none()
        if job is not None:
            from ..rights import assert_patient_writable

            assert_patient_writable(session, clinic_id, job.patient_id)
        existing = _find_existing(session, effect_type, idempotency_key)
        if existing is not None:
            _ensure_same_request(existing, request_hash)
            return existing, False

        effect = ExternalEffect(
            id=f"effect-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            aggregate_type=_OUTREACH_AGGREGATE_TYPE,
            aggregate_id=outreach_job_id,
            effect_type=effect_type,
            idempotency_key=idempotency_key,
            callback_token=generate_effect_token(clinic_id),
            payload_version=_PAYLOAD_VERSION,
            payload=payload,
            request_hash=request_hash,
            state=ExternalEffectState.PENDING,
            available_at=available_at,
            max_attempts=max_attempts,
        )
        if session.bind is None or session.bind.dialect.name != "postgresql":
            session.add(effect)
            session.flush()
            return effect, True

        savepoint = session.begin_nested()
        try:
            session.add(effect)
            session.flush()
        except IntegrityError:
            savepoint.rollback()
            existing = _find_existing(session, effect_type, idempotency_key)
            if existing is None:
                raise
            _ensure_same_request(existing, request_hash)
            return existing, False
        else:
            savepoint.commit()
            return effect, True


def _find_existing(
    session: Session,
    effect_type: ExternalEffectType,
    idempotency_key: str,
) -> ExternalEffect | None:
    return session.execute(
        tenant_select(ExternalEffect).where(
            ExternalEffect.effect_type == effect_type,
            ExternalEffect.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()


def _ensure_same_request(effect: ExternalEffect, request_hash: str) -> None:
    if effect.request_hash != request_hash:
        raise ValueError("idempotency key is already bound to a different request")


def _request_hash(
    *,
    aggregate_type: str,
    aggregate_id: str,
    effect_type: ExternalEffectType,
    payload_version: int,
    payload: dict[str, object],
) -> str:
    encoded = json.dumps(
        {
            "aggregate_id": aggregate_id,
            "aggregate_type": aggregate_type,
            "effect_type": effect_type.value,
            "payload": payload,
            "payload_version": payload_version,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()