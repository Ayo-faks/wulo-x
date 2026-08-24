"""Audit helpers for deterministic messaging actions."""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy.orm import Session

from ..enums import AuditAction
from ..models import AuditLog

ACTOR = "system:messaging"


def audit_action(
    session: Session,
    clinic_id: str,
    action: AuditAction,
    entity_ref: str,
    payload: dict[str, object],
    actor: str = ACTOR,
) -> None:
    """Append one immutable audit-log row with a hashed non-PII payload."""
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    session.add(
        AuditLog(
            id=f"audit-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            actor=actor,
            action=action,
            entity_ref=entity_ref,
            payload_hash=hashlib.sha256(encoded).hexdigest(),
        )
    )