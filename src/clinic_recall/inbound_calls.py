"""Inbound call lifecycle updates and minimized outcome logging."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .enums import AuditAction, InboundCallStatus
from .messaging.audit import audit_action
from .models import InboundCall


def log_inbound_call_outcome(
    session: Session,
    clinic_id: str,
    *,
    inbound_call_id: str,
    outcome: str,
    now: datetime,
    summary: str = "",
) -> dict[str, Any]:
    """Record a minimized inbound call outcome."""
    _require_aware("now", now)
    cleaned_outcome = str(outcome or "").strip()[:64]
    if not cleaned_outcome:
        raise ValueError("outcome is required")
    with clinic_scope(session, clinic_id):
        call = session.execute(
            tenant_select(InboundCall).where(InboundCall.id == inbound_call_id)
        ).scalar_one_or_none()
        if call is None:
            raise LookupError("inbound call not found for trusted clinic context")
        call.outcome = cleaned_outcome
        call.status = InboundCallStatus.COMPLETED
        audit_action(
            session,
            clinic_id,
            AuditAction.PLACE_CALL,
            call.id,
            {"outcome": cleaned_outcome, "summary": summary[:500], "occurred_at": now},
            actor="system:inbound-assistant",
        )
        session.flush()
        return {"inbound_call_id": call.id, "outcome": call.outcome, "status": call.status.value}


def record_consent_decision(
    session: Session,
    clinic_id: str,
    *,
    inbound_call_id: str,
    consent_type: str,
    granted: bool,
    now: datetime,
) -> dict[str, Any]:
    """Record a bounded non-recording contact-consent decision."""
    _require_aware("now", now)
    cleaned_type = str(consent_type or "").strip().lower()
    if cleaned_type == "recording":
        raise ValueError("recording consent is not model-callable")
    if cleaned_type != "contact":
        raise ValueError("unsupported consent type")
    with clinic_scope(session, clinic_id):
        call = session.execute(
            tenant_select(InboundCall).where(InboundCall.id == inbound_call_id)
        ).scalar_one_or_none()
        if call is None:
            raise LookupError("inbound call not found for trusted clinic context")
        metadata = dict(call.provider_metadata or {})
        consent = dict(metadata.get("consent") or {})
        consent[cleaned_type] = bool(granted)
        metadata["consent"] = consent
        call.provider_metadata = metadata
        audit_action(
            session,
            clinic_id,
            AuditAction.PLACE_CALL,
            call.id,
            {"consent_type": cleaned_type, "granted": bool(granted), "occurred_at": now},
            actor="system:inbound-assistant",
        )
        session.flush()
        return {"inbound_call_id": call.id, "consent_type": cleaned_type, "granted": bool(granted)}


def _require_aware(field: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")