"""Deterministic retention minimisation jobs for Clinic Recall."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .durable.enqueue import enqueue_rights_effect
from .enums import (
    AuditAction,
    RightsRequestKind,
    RightsRequestState,
    RightsTargetAction,
    RightsTargetOwnerType,
    RightsTargetResource,
    RightsTargetState,
    RightsTargetSystem,
)
from .messaging.audit import audit_action
from .models import Clinic, Interaction, RightsRequest, RightsTarget
from .rights import SubjectKey, SubjectKeyring


@dataclass(frozen=True)
class RetentionPolicy:
    """Trusted, versioned retention authority supplied by configuration."""

    version: str
    approval_evidence_hash: str
    approved_at: datetime
    effective_at: datetime
    expires_at: datetime
    retain_for: timedelta
    request_due_after: timedelta

    def __post_init__(self) -> None:
        if not self.version.strip() or len(self.version) > 128:
            raise ValueError("retention policy version must contain 1 to 128 characters")
        _require_sha256(self.approval_evidence_hash, "approval_evidence_hash")
        approved_at = _aware_utc(self.approved_at, "approved_at")
        effective_at = _aware_utc(self.effective_at, "effective_at")
        expires_at = _aware_utc(self.expires_at, "expires_at")
        if approved_at > effective_at:
            raise ValueError("retention policy approval must not follow its effective time")
        if effective_at >= expires_at:
            raise ValueError("retention policy expiry must follow its effective time")
        if not isinstance(self.retain_for, timedelta) or self.retain_for <= timedelta(0):
            raise ValueError("retention policy retain_for must be positive")
        if (
            not isinstance(self.request_due_after, timedelta)
            or self.request_due_after <= timedelta(0)
        ):
            raise ValueError("retention policy request_due_after must be positive")


@dataclass(frozen=True)
class RetentionScheduleResult:
    """Aggregate-only outcome from one retention scheduling pass."""

    created_count: int
    existing_count: int


def schedule_retention_requests(
    session: Session,
    *,
    clinic_id: str,
    keyring: SubjectKeyring,
    policy: RetentionPolicy | None,
    now: datetime,
    enabled: bool = False,
    limit: int = 100,
) -> RetentionScheduleResult:
    """Inventory due interaction content into durable minimisation requests."""
    now = _aware_utc(now, "now")
    if enabled is not True:
        raise RuntimeError("retention scheduling is disabled")
    if policy is None:
        raise ValueError("retention policy is required")
    _validate_policy_at(policy, now)
    if not clinic_id:
        raise ValueError("clinic_id is required")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")

    created_count = 0
    existing_count = 0
    cutoff = now - policy.retain_for
    with clinic_scope(session, clinic_id):
        clinic = session.get(Clinic, clinic_id)
        if clinic is None:
            raise LookupError(f"clinic {clinic_id!r} not found")
        interactions = list(
            session.execute(
                tenant_select(Interaction)
                .where(
                    Interaction.content.is_not(None),
                    Interaction.occurred_at <= cutoff,
                )
                .order_by(Interaction.occurred_at, Interaction.id)
                .limit(limit)
            ).scalars()
        )
        for interaction in interactions:
            retention_deadline = _as_utc(interaction.occurred_at) + policy.retain_for
            scope_hashes = {
                _retention_hash(key, "scope", clinic_id, interaction.id)
                for key in keyring.keys
            }
            _lock_retention_scope(session, clinic_id, interaction.id)
            request = session.execute(
                tenant_select(RightsRequest).where(
                    RightsRequest.kind == RightsRequestKind.RETENTION,
                    RightsRequest.scope_hash.in_(scope_hashes),
                )
            ).scalar_one_or_none()
            if request is None:
                request = _create_retention_request(
                    session,
                    clinic_id=clinic_id,
                    interaction=interaction,
                    keyring=keyring,
                    policy=policy,
                    now=now,
                    retention_deadline=retention_deadline,
                )
                created_count += 1
            else:
                existing_count += 1
            _converge_retention_target(
                session,
                clinic_id=clinic_id,
                request=request,
                interaction=interaction,
                keyring=keyring,
                now=now,
            )
        session.flush()
    return RetentionScheduleResult(
        created_count=created_count,
        existing_count=existing_count,
    )


def _create_retention_request(
    session: Session,
    *,
    clinic_id: str,
    interaction: Interaction,
    keyring: SubjectKeyring,
    policy: RetentionPolicy,
    now: datetime,
    retention_deadline: datetime,
) -> RightsRequest:
    key = keyring.current
    scope_hash = _retention_hash(key, "scope", clinic_id, interaction.id)
    request = RightsRequest(
        id=f"rights-{uuid.uuid4().hex}",
        clinic_id=clinic_id,
        kind=RightsRequestKind.RETENTION,
        subject_key_hash=_retention_hash(
            key,
            "subject",
            clinic_id,
            interaction.id,
        ),
        subject_key_version=key.version,
        patient_reference_hash=_retention_hash(
            key,
            "owner-reference",
            clinic_id,
            interaction.id,
        ),
        patient_id=None,
        request_identity_hash=_retention_hash(
            key,
            "request-identity",
            clinic_id,
            policy.version,
        ),
        actor_role="system",
        actor_reference_hash=_retention_hash(
            key,
            "actor-reference",
            clinic_id,
            "retention-scheduler",
        ),
        policy_version=policy.version,
        approval_evidence_hash=policy.approval_evidence_hash,
        scope_hash=scope_hash,
        state=RightsRequestState.FROZEN,
        requested_at=now,
        frozen_at=now,
        inventory_finalized_at=now,
        due_at=retention_deadline + policy.request_due_after,
        target_count=1,
    )
    session.add(request)
    session.flush()
    audit_action(
        session,
        clinic_id,
        AuditAction.RETENTION_PURGE,
        request.id,
        {
            "request_id": request.id,
            "created": True,
            "policy_version": policy.version,
            "occurred_at": now,
        },
        actor="system:retention",
    )
    return request


def _converge_retention_target(
    session: Session,
    *,
    clinic_id: str,
    request: RightsRequest,
    interaction: Interaction,
    keyring: SubjectKeyring,
    now: datetime,
) -> RightsTarget:
    target_hashes = {
        _retention_hash(key, "target", clinic_id, interaction.id)
        for key in keyring.keys
    }
    target = session.execute(
        tenant_select(RightsTarget).where(
            RightsTarget.request_id == request.id,
            RightsTarget.target_key_hash.in_(target_hashes),
        )
    ).scalar_one_or_none()
    if target is None:
        target = RightsTarget(
            id=f"rights-target-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            request_id=request.id,
            system=RightsTargetSystem.LOCAL,
            resource=RightsTargetResource.INTERACTION_CONTENT,
            action=RightsTargetAction.MINIMIZE,
            owner_type=RightsTargetOwnerType.INTERACTION,
            owner_id=interaction.id,
            target_key_hash=_retention_hash(
                keyring.current,
                "target",
                clinic_id,
                interaction.id,
            ),
            mandatory=True,
            state=RightsTargetState.REQUESTED,
            attempt_ordinal=1,
            available_at=now,
            due_at=_as_utc(request.due_at),
        )
        session.add(target)
        session.flush()
    if (
        target.current_effect_id is None
        and target.state == RightsTargetState.REQUESTED
    ):
        target.attempt_ordinal = max(target.attempt_ordinal, 1)
        effect, _ = enqueue_rights_effect(
            session,
            clinic_id=clinic_id,
            target_id=target.id,
            attempt_ordinal=target.attempt_ordinal,
            available_at=now,
        )
        target.current_effect_id = effect.id
    request.target_count = 1
    return target


def _validate_policy_at(policy: RetentionPolicy, now: datetime) -> None:
    if _aware_utc(policy.approved_at, "approved_at") > now:
        raise ValueError("retention policy approval is in the future")
    if _aware_utc(policy.effective_at, "effective_at") > now:
        raise ValueError("retention policy is not yet effective")
    if _aware_utc(policy.expires_at, "expires_at") <= now:
        raise ValueError("retention policy is expired")


def _retention_hash(
    key: SubjectKey,
    domain: str,
    clinic_id: str,
    value: str,
) -> str:
    payload = "\x00".join(("clinic-recall-retention-v1", domain, clinic_id, value))
    return hmac.new(key.secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _lock_retention_scope(
    session: Session,
    clinic_id: str,
    interaction_id: str,
) -> None:
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(
        "\x00".join(
            ("clinic-recall-retention-lock-v1", clinic_id, interaction_id)
        ).encode("utf-8")
    ).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    advisory_key = unsigned if unsigned < 2**63 else unsigned - 2**64
    session.execute(
        sa.text("SELECT pg_advisory_xact_lock(:advisory_key)"),
        {"advisory_key": advisory_key},
    )


def _require_sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a 64-character SHA-256 hex digest")


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)