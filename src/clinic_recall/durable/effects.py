"""State transitions and row-leased claiming for external effects."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..db import clinic_scope, tenant_select
from ..enums import ExternalEffectState, ExternalEffectType
from ..handoffs import ensure_external_effect_handoff
from ..models import ExternalEffect

RETRY_BASE_DELAY = timedelta(minutes=1)
RETRY_MAX_DELAY = timedelta(hours=1)


def claim_effects(
    session: Session,
    *,
    clinic_id: str,
    worker_id: str,
    now: datetime,
    lease_for: timedelta,
    limit: int = 1,
    effect_types: Collection[ExternalEffectType] | None = None,
) -> list[ExternalEffect]:
    """Claim due work without replaying an ambiguously dispatched effect."""
    _require_aware(now, "now")
    if not clinic_id or not worker_id:
        raise ValueError("clinic_id and worker_id are required")
    if lease_for <= timedelta(0):
        raise ValueError("lease_for must be positive")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    normalized_effect_types = tuple(effect_types) if effect_types is not None else None
    if normalized_effect_types == ():
        raise ValueError("effect_types must not be empty")
    effect_type_filter = (
        ExternalEffect.effect_type.in_(normalized_effect_types)
        if normalized_effect_types is not None
        else sa.true()
    )

    with clinic_scope(session, clinic_id):
        session.execute(
            sa.update(ExternalEffect)
            .where(
                ExternalEffect.clinic_id == clinic_id,
                ExternalEffect.state == ExternalEffectState.DISPATCHING,
                effect_type_filter,
                ExternalEffect.lease_expires_at.is_not(None),
                ExternalEffect.lease_expires_at <= now,
            )
            .values(
                state=ExternalEffectState.RECONCILE_REQUIRED,
                lease_owner=None,
                lease_expires_at=None,
                last_error_class="AmbiguousDispatch",
                last_error_code="dispatch_lease_expired",
            )
        )

        statement = (
            tenant_select(ExternalEffect)
            .where(
                sa.or_(
                    sa.and_(
                        ExternalEffect.state == ExternalEffectState.PENDING,
                        ExternalEffect.available_at <= now,
                    ),
                    sa.and_(
                        ExternalEffect.state == ExternalEffectState.LEASED,
                        ExternalEffect.lease_expires_at.is_not(None),
                        ExternalEffect.lease_expires_at <= now,
                    ),
                ),
                effect_type_filter,
                ExternalEffect.attempt_count < ExternalEffect.max_attempts,
            )
            .order_by(ExternalEffect.available_at, ExternalEffect.created_at, ExternalEffect.id)
            .limit(limit)
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)

        effects = list(session.execute(statement).scalars())
        lease_expires_at = now + lease_for
        for effect in effects:
            effect.state = ExternalEffectState.LEASED
            effect.lease_owner = worker_id
            effect.lease_expires_at = lease_expires_at
        session.flush()
        return effects


def mark_dispatching(
    session: Session,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
) -> ExternalEffect:
    """Mark a leased effect dispatching before any provider request occurs."""
    _require_aware(now, "now")
    with clinic_scope(session, clinic_id):
        statement = tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        effect = session.execute(statement).scalar_one_or_none()
        if effect is None:
            raise LookupError(f"external effect {effect_id!r} not found for clinic")
        if effect.state != ExternalEffectState.LEASED:
            raise ValueError("external effect must be leased before dispatch")
        if effect.lease_owner != worker_id:
            raise ValueError("external effect lease is owned by another worker")
        if effect.lease_expires_at is None or _as_utc(effect.lease_expires_at) <= now:
            raise ValueError("external effect lease has expired")
        if effect.attempt_count >= effect.max_attempts:
            raise ValueError("external effect has exhausted its dispatch attempts")

        effect.state = ExternalEffectState.DISPATCHING
        effect.dispatch_started_at = effect.dispatch_started_at or now
        effect.attempt_count += 1
        session.flush()
        return effect


def lock_dispatching_effect(
    session: Session,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
) -> ExternalEffect:
    """Hold a no-key-update lock while provider I/O and domain writes run."""
    effect = _load_effect_for_update(
        session,
        clinic_id,
        effect_id,
        no_key_update=True,
    )
    _require_owned_dispatch(effect, worker_id)
    return effect


def mark_succeeded(
    session: Session,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
    provider_resource_id: str,
) -> ExternalEffect:
    """Complete a dispatch only when the provider returned a resource identity."""
    _require_aware(now, "now")
    if not provider_resource_id:
        raise ValueError("provider_resource_id is required for success")
    effect = _load_effect_for_update(session, clinic_id, effect_id)
    if effect.state == ExternalEffectState.SUCCEEDED:
        if effect.provider_resource_id != provider_resource_id:
            effect.state = ExternalEffectState.RECONCILE_REQUIRED
            effect.last_error_class = "ProviderCallbackConflict"
            effect.last_error_code = "provider_identity_conflict"
            effect.lease_owner = None
            effect.lease_expires_at = None
            session.flush()
            return effect
        if not effect.provider_status:
            effect.provider_status = "accepted"
        if not effect.completion_evidence_hash:
            effect.completion_evidence_hash = _completion_hash(
                effect.request_hash,
                provider_resource_id,
                effect.provider_status,
            )
        session.flush()
        return effect
    if effect.state == ExternalEffectState.RECONCILE_REQUIRED:
        return effect
    _require_owned_dispatch(effect, worker_id)
    effect.state = ExternalEffectState.SUCCEEDED
    effect.provider_resource_id = provider_resource_id
    effect.provider_status = "accepted"
    effect.completion_evidence_hash = _completion_hash(
        effect.request_hash,
        provider_resource_id,
        effect.provider_status,
    )
    _finish(effect, now)
    session.flush()
    return effect


def mark_rejected(
    session: Session,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
    reason_code: str = "provider_rejected",
) -> ExternalEffect:
    """Record a definitive provider rejection without retaining provider text."""
    _require_aware(now, "now")
    if not reason_code:
        raise ValueError("reason_code is required")
    effect = _load_owned_dispatch(session, clinic_id, effect_id, worker_id)
    effect.state = ExternalEffectState.REJECTED
    effect.provider_status = "rejected"
    effect.last_error_class = "ProviderRejected"
    effect.last_error_code = reason_code
    _finish(effect, now)
    session.flush()
    return effect


def mark_retryable_failure(
    session: Session,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
    reason_code: str,
    base_delay: timedelta = RETRY_BASE_DELAY,
    max_delay: timedelta = RETRY_MAX_DELAY,
    not_before: datetime | None = None,
    provider_dispatch_started: bool = True,
    failure_class: str = "ProviderTransientFailure",
) -> tuple[ExternalEffect, bool]:
    """Return a definitive transient failure to pending at persisted backoff."""
    _require_aware(now, "now")
    if not reason_code:
        raise ValueError("reason_code is required")
    if base_delay <= timedelta(0) or max_delay < base_delay:
        raise ValueError("retry delays must be positive and bounded")
    if not failure_class:
        raise ValueError("failure_class is required")
    if not_before is not None:
        _require_aware(not_before, "not_before")
    effect = _load_owned_dispatch(session, clinic_id, effect_id, worker_id)
    if not provider_dispatch_started and effect.last_error_class != "ProviderTransientFailure":
        effect.dispatch_started_at = None
    if effect.attempt_count >= effect.max_attempts:
        effect.state = ExternalEffectState.DEAD_LETTER
        effect.provider_status = "not_dispatched"
        effect.last_error_class = "RetryExhausted"
        effect.last_error_code = reason_code
        _finish(effect, now)
        _handoff, handoff_created = ensure_external_effect_handoff(
            session,
            effect,
            reason_code="retry_exhausted",
            now=now,
        )
        return effect, handoff_created
    multiplier = 2 ** max(effect.attempt_count - 1, 0)
    delay = min(base_delay * multiplier, max_delay)
    effect.state = ExternalEffectState.PENDING
    available_at = now + delay
    if not_before is not None and _as_utc(not_before) > _as_utc(available_at):
        available_at = not_before
    effect.available_at = available_at
    effect.provider_status = "retry_scheduled"
    effect.last_error_class = failure_class
    effect.last_error_code = reason_code
    effect.lease_owner = None
    effect.lease_expires_at = None
    session.flush()
    return effect, False


def mark_canceled(
    session: Session,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
    reason_code: str,
) -> ExternalEffect:
    """Cancel work that a fresh deterministic gate no longer permits."""
    _require_aware(now, "now")
    if not reason_code:
        raise ValueError("reason_code is required")
    effect = _load_owned_dispatch(session, clinic_id, effect_id, worker_id)
    effect.state = ExternalEffectState.CANCELED
    effect.provider_status = "not_dispatched"
    effect.last_error_class = "DispatchCanceled"
    effect.last_error_code = reason_code
    _finish(effect, now)
    session.flush()
    return effect


def cancel_undispatched_effects(
    session: Session,
    *,
    clinic_id: str,
    aggregate_type: str,
    aggregate_id: str,
    effect_type: ExternalEffectType,
    now: datetime,
    reason_code: str,
) -> int:
    """Cancel matching pending or leased work before provider dispatch starts."""
    _require_aware(now, "now")
    if not aggregate_type or not aggregate_id or not reason_code:
        raise ValueError("aggregate_type, aggregate_id, and reason_code are required")
    with clinic_scope(session, clinic_id):
        statement = tenant_select(ExternalEffect).where(
            ExternalEffect.aggregate_type == aggregate_type,
            ExternalEffect.aggregate_id == aggregate_id,
            ExternalEffect.effect_type == effect_type,
            ExternalEffect.state.in_(
                {ExternalEffectState.PENDING, ExternalEffectState.LEASED}
            ),
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        effects = list(session.execute(statement).scalars())
        for effect in effects:
            effect.state = ExternalEffectState.CANCELED
            effect.provider_status = "not_dispatched"
            effect.last_error_class = "DispatchCanceled"
            effect.last_error_code = reason_code
            effect.completed_at = now
            effect.lease_owner = None
            effect.lease_expires_at = None
        session.flush()
        return len(effects)


def mark_reconcile_required(
    session: Session,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
    reason_code: str = "provider_outcome_unknown",
) -> ExternalEffect:
    """Quarantine an ambiguous dispatch so no worker can replay it."""
    _require_aware(now, "now")
    effect = _load_owned_dispatch(session, clinic_id, effect_id, worker_id)
    effect.state = ExternalEffectState.RECONCILE_REQUIRED
    effect.last_error_class = "ProviderDispatchError"
    effect.last_error_code = reason_code
    effect.lease_owner = None
    effect.lease_expires_at = None
    session.flush()
    return effect


def _load_owned_dispatch(
    session: Session,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
) -> ExternalEffect:
    effect = _load_effect_for_update(session, clinic_id, effect_id)
    _require_owned_dispatch(effect, worker_id)
    return effect


def _load_effect_for_update(
    session: Session,
    clinic_id: str,
    effect_id: str,
    *,
    no_key_update: bool = False,
) -> ExternalEffect:
    with clinic_scope(session, clinic_id):
        statement = tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update(key_share=no_key_update)
        effect = session.execute(statement).scalar_one_or_none()
    if effect is None:
        raise LookupError(f"external effect {effect_id!r} not found for clinic")
    return effect


def _require_owned_dispatch(effect: ExternalEffect, worker_id: str) -> None:
    if effect.state != ExternalEffectState.DISPATCHING:
        raise ValueError("external effect must be dispatching")
    if effect.lease_owner != worker_id:
        raise ValueError("external effect dispatch is owned by another worker")


def _finish(effect: ExternalEffect, now: datetime) -> None:
    effect.completed_at = now
    effect.lease_owner = None
    effect.lease_expires_at = None


def _completion_hash(request_hash: str, provider_resource_id: str, status: str) -> str:
    encoded = json.dumps(
        {
            "provider_resource_id": provider_resource_id,
            "request_hash": request_hash,
            "status": status,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)