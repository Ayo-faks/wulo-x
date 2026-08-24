"""Runtime identity dependencies; no clinic/DPO policy is currently approved."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import Any

from .enums import Channel
from .identity_evidence import (
    IdentityAuthorizationContext,
    IdentityEvidenceService,
)


def runtime_identity_service(now: datetime) -> IdentityEvidenceService:
    """Return a fail-closed service until a reviewed policy provider exists."""
    return IdentityEvidenceService(
        policy=None,
        clock=lambda: now,
        identifier_factory=lambda: f"identity-evidence-{uuid.uuid4().hex}",
        challenge_factory=lambda: secrets.token_urlsafe(32),
    )


def trusted_identity_context(
    args: dict[str, Any],
    *,
    channel: Channel,
) -> IdentityAuthorizationContext | None:
    """Build context only from underscore-prefixed server-injected values."""
    evidence_id = str(args.get("_identity_evidence_id") or "").strip()
    session_id = str(args.get("_identity_session_id") or "").strip()
    route_id = str(args.get("_identity_route_id") or "").strip()
    if not evidence_id or not session_id or not route_id:
        return None
    return IdentityAuthorizationContext(
        evidence_id=evidence_id,
        session_id=session_id,
        route_id=route_id,
        channel=channel,
    )


__all__ = ["runtime_identity_service", "trusted_identity_context"]