"""Provider-neutral inbound phone routing for Clinic Recall."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .enums import (
    ClinicPhoneProvider,
    ClinicPhonePurpose,
    ClinicPhoneStatus,
    InboundCallStatus,
    InteractionDirection,
)
from .models import ClinicPhoneNumber, InboundCall
from .recording import ensure_call_record

INBOUND_SOURCE = "clinic_recall_inbound"
INBOUND_SCENARIO = "inbound_clinic"


@dataclass(frozen=True)
class InboundCallContext:
    """Trusted context emitted by a provider webhook for a VoiceLive session."""

    clinic_id: str
    inbound_call_id: str
    called_number_id: str
    called_number: str
    provider: ClinicPhoneProvider
    provider_call_id: str
    caller_number_hash: str | None
    record_call: bool
    scenario: str = INBOUND_SCENARIO
    source: str = INBOUND_SOURCE
    call_direction: str = "inbound"

    def stream_parameters(self) -> dict[str, str]:
        """Return Twilio/VoiceLive-safe string parameters for media streams."""
        params = {
            "source": self.source,
            "provider": self.provider.value,
            "provider_call_id": self.provider_call_id,
            "inbound_call_id": self.inbound_call_id,
            "clinic_id": self.clinic_id,
            "scenario": self.scenario,
            "call_direction": self.call_direction,
            "called_number_id": self.called_number_id,
            "called_number": self.called_number,
            "record_call": "true" if self.record_call else "false",
        }
        if self.caller_number_hash:
            params["caller_number_hash"] = self.caller_number_hash
        return params


class InboundRouteError(ValueError):
    """Raised when an inbound provider call cannot be routed safely."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def prepare_inbound_call(
    session: Session,
    *,
    provider: ClinicPhoneProvider | str,
    provider_call_id: str,
    called_number: str,
    caller_number: str | None,
    now: datetime,
    metadata: dict[str, Any] | None = None,
) -> InboundCallContext:
    """Resolve a called clinic number and persist idempotent inbound call context."""
    _require_aware("now", now)
    resolved_provider = _coerce_provider(provider)
    normalized_called_number = normalize_phone_number(called_number)
    normalized_caller_number = normalize_phone_number(caller_number) if caller_number else None
    provider_call_id = provider_call_id.strip()
    if not provider_call_id:
        raise InboundRouteError("missing_provider_call_id", "provider_call_id is required")

    route = _resolve_active_route(session, resolved_provider, normalized_called_number)
    caller_hash = _hash_caller_number(normalized_caller_number, route.clinic_id)
    with clinic_scope(session, route.clinic_id):
        existing = session.execute(
            tenant_select(InboundCall).where(
                InboundCall.provider == resolved_provider,
                InboundCall.provider_call_id == provider_call_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = InboundCall(
                id=f"inbound-call-{uuid.uuid4().hex}",
                clinic_id=route.clinic_id,
                clinic_phone_number_id=route.id,
                provider=resolved_provider,
                provider_call_id=provider_call_id,
                called_number=normalized_called_number,
                caller_number_hash=caller_hash,
                status=InboundCallStatus.STARTED,
                provider_metadata=_minimized_metadata(metadata),
            )
            session.add(existing)
        else:
            existing.clinic_phone_number_id = existing.clinic_phone_number_id or route.id
            existing.called_number = existing.called_number or normalized_called_number
            existing.caller_number_hash = existing.caller_number_hash or caller_hash
        session.flush()
        ensure_call_record(
            session,
            route.clinic_id,
            provider=resolved_provider,
            provider_call_id=(
                provider_call_id
                if resolved_provider == ClinicPhoneProvider.TWILIO
                else None
            ),
            inbound_call_id=existing.id,
            session_id=existing.id,
            direction=InteractionDirection.INBOUND,
            scenario=INBOUND_SCENARIO,
            patient_id=None,
            consent_snapshot=None,
            now=now,
        )

        return InboundCallContext(
            clinic_id=route.clinic_id,
            inbound_call_id=existing.id,
            called_number_id=route.id,
            called_number=normalized_called_number,
            provider=resolved_provider,
            provider_call_id=provider_call_id,
            caller_number_hash=caller_hash,
            record_call=False,
        )


def normalize_phone_number(value: str) -> str:
    """Normalize provider phone numbers to a conservative E.164-like form."""
    raw = str(value or "").strip()
    if not raw:
        raise InboundRouteError("missing_phone_number", "phone number is required")
    if raw.startswith("+"):
        prefix = "+"
        body = raw[1:]
    elif raw.startswith("00"):
        prefix = "+"
        body = raw[2:]
    else:
        prefix = ""
        body = raw
    digits = re.sub(r"\D+", "", body)
    if not digits:
        raise InboundRouteError("invalid_phone_number", "phone number has no digits")
    return f"{prefix}{digits}"


def hash_phone_number_for_clinic(phone_number: str | None, clinic_id: str) -> str | None:
    """Hash a normalized caller number with clinic scope for patient matching."""
    normalized = normalize_phone_number(phone_number) if phone_number else None
    return _hash_caller_number(normalized, clinic_id)


def _resolve_active_route(
    session: Session, provider: ClinicPhoneProvider, phone_number: str
) -> ClinicPhoneNumber:
    route = session.execute(
        sa.select(ClinicPhoneNumber).where(
            ClinicPhoneNumber.provider == provider,
            ClinicPhoneNumber.phone_number == phone_number,
        )
    ).scalar_one_or_none()
    if route is None:
        raise InboundRouteError("unmapped_called_number", "called number is not mapped")
    if route.status != ClinicPhoneStatus.ACTIVE:
        raise InboundRouteError("inactive_called_number", "called number is not active")
    if route.purpose not in {ClinicPhonePurpose.INBOUND, ClinicPhonePurpose.BOTH}:
        raise InboundRouteError("wrong_number_purpose", "called number is not enabled for inbound")
    return route


def _coerce_provider(provider: ClinicPhoneProvider | str) -> ClinicPhoneProvider:
    if isinstance(provider, ClinicPhoneProvider):
        return provider
    try:
        return ClinicPhoneProvider(str(provider).strip().lower())
    except ValueError as exc:
        raise InboundRouteError("unsupported_provider", f"unsupported provider: {provider!r}") from exc


def _hash_caller_number(phone_number: str | None, clinic_id: str) -> str | None:
    if not phone_number:
        return None
    salt = os.getenv("CLINIC_RECALL_CALLER_HASH_SALT", "")
    digest = hashlib.sha256(f"{salt}:{clinic_id}:{phone_number}".encode()).hexdigest()
    return f"sha256:{digest}"


def _minimized_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = metadata or {}
    allowed = ("AccountSid", "Direction", "ApiVersion")
    return {key: str(metadata[key]) for key in allowed if metadata.get(key) is not None}


def _require_aware(field: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")