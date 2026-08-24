"""Finite default-off worker for create-only Cliniko booking effects."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from ..config import ClinikoConfigurationError, get_cliniko_config
from ..db import clinic_scope, get_sessionmaker, tenant_select
from ..enums import (
    BookingWriteBackState,
    Channel,
    ExternalEffectState,
    ExternalEffectType,
)
from ..identity_evidence import IdentityEvidenceService
from ..models import BookingAction, ExternalEffect
from ..pilot_controls import (
    JobPilotGate,
    job_gate_for_snapshot,
    operational_switch_snapshot_from_environment,
)
from ..sync.cliniko_booking import ClinikoBookingClient
from ..sync.cliniko_client import (
    ClinikoAuthenticationError,
    ClinikoError,
    ClinikoNotFoundError,
    ClinikoRateLimitedError,
    ClinikoValidationError,
)
from ..telemetry import emit_worker_summary
from .cliniko_booking_state import (
    finalize_verified,
    load_dispatch_context,
    preflight_zero_match_hash,
)
from .config import (
    durable_booking_confirmation_enabled,
    durable_cliniko_write_enabled,
)
from .effects import (
    claim_effects,
    mark_dispatching,
    mark_reconcile_required,
    mark_rejected,
    mark_retryable_failure,
)
from .worker import (
    _bootstrap_runtime_configuration,
    _default_worker_id,
    _parse_now,
)

SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class ClinikoBookingRunResult:
    enabled: bool
    claimed: int = 0
    verified: int = 0
    rejected: int = 0
    conflicts: int = 0
    retried: int = 0
    dead_lettered: int = 0
    canceled: int = 0
    handoffs_queued: int = 0
    reconcile_required: int = 0
    recovered_dispatches: int = 0

    def as_summary(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "claimed": self.claimed,
            "verified": self.verified,
            "rejected": self.rejected,
            "conflicts": self.conflicts,
            "retried": self.retried,
            "dead_lettered": self.dead_lettered,
            "canceled": self.canceled,
            "handoffs_queued": self.handoffs_queued,
            "reconcile_required": self.reconcile_required,
            "recovered_dispatches": self.recovered_dispatches,
        }


def run_once(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    worker_id: str,
    client: ClinikoBookingClient,
    programme_gate: JobPilotGate,
    now: datetime,
    enabled: bool = False,
    confirmation_release_enabled: bool = False,
    lease_for: timedelta = timedelta(minutes=5),
    limit: int = 10,
    identity_service: IdentityEvidenceService | None = None,
) -> ClinikoBookingRunResult:
    """Claim, preflight, dispatch once, and quarantine every unknown outcome."""
    if not enabled:
        return ClinikoBookingRunResult(enabled=False)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    now = now.astimezone(UTC)
    with session_factory() as session:
        claimed = claim_effects(
            session,
            clinic_id=clinic_id,
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
            limit=limit,
            effect_types=(ExternalEffectType.CLINIKO_BOOKING,),
        )
        recovered_dispatches = _project_expired_dispatches(session, clinic_id)
        effect_ids = [effect.id for effect in claimed]
        session.commit()

    verified = 0
    rejected = 0
    conflicts = 0
    retried = 0
    dead_lettered = 0
    canceled = 0
    handoffs_queued = 0
    reconcile_required = 0
    for effect_id in effect_ids:
        try:
            with session_factory() as session:
                context = load_dispatch_context(
                    session,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    now=now,
                    programme_gate=programme_gate,
                    identity_service=identity_service,
                )
        except Exception:  # noqa: BLE001 - deterministic local gate failure
            _cancel_leased(session_factory, clinic_id, effect_id, "dispatch_gate_blocked")
            canceled += 1
            continue

        try:
            if not client.exact_slot_is_available(context.expected):
                if _mark_conflict(
                    session_factory,
                    clinic_id,
                    effect_id,
                    reason_code="slot_no_longer_available",
                    dispatched=False,
                    now=now,
                ):
                    handoffs_queued += 1
                conflicts += 1
                continue
            preexisting = tuple(
                candidate
                for candidate in client.list_signature_candidates(context.expected)
                if candidate.matches(context.expected)
            )
        except ClinikoRateLimitedError as error:
            exhausted, handoff = _retry_preflight_read(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                now=now,
                not_before=error.reset_at,
            )
            if exhausted:
                rejected += 1
                dead_lettered += 1
            else:
                retried += 1
            handoffs_queued += int(handoff)
            continue
        except ClinikoError:
            exhausted, handoff = _retry_preflight_read(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                now=now,
            )
            if exhausted:
                rejected += 1
                dead_lettered += 1
            else:
                retried += 1
            handoffs_queued += int(handoff)
            continue
        if preexisting:
            if _mark_conflict(
                session_factory,
                clinic_id,
                effect_id,
                reason_code="exact_match_already_exists",
                dispatched=False,
                now=now,
            ):
                handoffs_queued += 1
            conflicts += 1
            continue

        try:
            with session_factory() as session:
                current = load_dispatch_context(
                    session,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    now=now,
                    programme_gate=programme_gate,
                    identity_service=identity_service,
                )
                if current != context:
                    raise ValueError("booking_context_changed")
                effect = mark_dispatching(
                    session,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    worker_id=worker_id,
                    now=now,
                )
                with clinic_scope(session, clinic_id):
                    action = session.execute(
                        tenant_select(BookingAction).where(BookingAction.id == context.action_id)
                    ).scalar_one()
                    action.write_back_state = BookingWriteBackState.DISPATCHING
                    action.provider_attempted_at = action.provider_attempted_at or now
                    effect.preflight_evidence_hash = preflight_zero_match_hash(
                        effect.request_hash
                    )
                    effect.settle_deadline_at = effect.settle_deadline_at or (
                        now + timedelta(minutes=5)
                    )
                session.commit()
        except Exception:  # noqa: BLE001 - no provider request occurred
            _cancel_leased(session_factory, clinic_id, effect_id, "dispatch_prepare_failed")
            canceled += 1
            continue

        try:
            accepted = client.create_individual_appointment(context.expected)
        except ClinikoRateLimitedError as error:
            exhausted, handoff = _settle_rate_limit(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                reset_at=error.reset_at,
            )
            if exhausted:
                rejected += 1
                dead_lettered += 1
            else:
                retried += 1
            handoffs_queued += int(handoff)
            continue
        except (
            ClinikoAuthenticationError,
            ClinikoNotFoundError,
            ClinikoValidationError,
        ):
            if _settle_rejected(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
            ):
                handoffs_queued += 1
            rejected += 1
            continue
        except Exception:  # noqa: BLE001 - provider contact outcome is unknown
            _settle_ambiguity(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
            )
            reconcile_required += 1
            continue

        try:
            _persist_accepted_identity(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                provider_id=accepted.provider_id,
            )
            observed = client.get_individual_appointment(accepted.provider_id)
        except Exception:  # noqa: BLE001 - accepted write lacks exact read-back
            _settle_ambiguity(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                reason_code="immediate_read_back_failed",
            )
            reconcile_required += 1
            continue
        if not observed.matches(context.expected):
            if _mark_conflict(
                session_factory,
                clinic_id,
                effect_id,
                reason_code="read_back_mismatch",
                dispatched=True,
                now=now,
                worker_id=worker_id,
            ):
                handoffs_queued += 1
            conflicts += 1
            continue

        try:
            with session_factory() as session:
                if finalize_verified(
                    session,
                    clinic_id=clinic_id,
                    context=context,
                    observed=observed,
                    now=now,
                    programme_gate=programme_gate,
                    confirmation_release_enabled=confirmation_release_enabled,
                    identity_service=identity_service,
                ):
                    verified += 1
                session.commit()
        except Exception:  # noqa: BLE001 - settlement after provider contact is unknown
            _settle_ambiguity(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                reason_code="verification_settlement_failed",
            )
            reconcile_required += 1
    result = ClinikoBookingRunResult(
        enabled=True,
        claimed=len(effect_ids),
        verified=verified,
        rejected=rejected,
        conflicts=conflicts,
        retried=retried,
        dead_lettered=dead_lettered,
        canceled=canceled,
        handoffs_queued=handoffs_queued,
        reconcile_required=reconcile_required,
        recovered_dispatches=recovered_dispatches,
    )
    emit_worker_summary("cliniko_dispatch", result.as_summary())
    return result


def _mark_conflict(
    session_factory: SessionFactory,
    clinic_id: str,
    effect_id: str,
    *,
    reason_code: str,
    dispatched: bool,
    now: datetime,
    worker_id: str | None = None,
) -> tuple[bool, bool]:
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            effect = session.execute(
                tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
            ).scalar_one()
            action = session.execute(
                tenant_select(BookingAction).where(BookingAction.id == effect.aggregate_id)
            ).scalar_one()
            if dispatched:
                if worker_id is None:
                    raise ValueError("worker_id is required for dispatched conflict")
                mark_reconcile_required(
                    session,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    worker_id=worker_id,
                    now=now,
                    reason_code=reason_code,
                )
                effect.last_error_class = "ReadBackConflict"
            else:
                effect.state = ExternalEffectState.REJECTED
                effect.provider_status = "not_dispatched"
                effect.last_error_class = "PreflightConflict"
                effect.last_error_code = reason_code
                effect.completed_at = now
                effect.lease_owner = None
                effect.lease_expires_at = None
            action.write_back_state = BookingWriteBackState.CONFLICT
            action.conflict_reason = reason_code
            handoff_created = _ensure_handoff(session, effect, reason_code, now)
        session.commit()
        return handoff_created


def _retry_preflight_read(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    now: datetime,
    not_before: datetime | None = None,
) -> tuple[bool, bool]:
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            effect = session.execute(
                tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
            ).scalar_one()
            action = session.execute(
                tenant_select(BookingAction).where(BookingAction.id == effect.aggregate_id)
            ).scalar_one()
            effect.read_attempt_count += 1
            effect.last_error_class = "PreflightReadError"
            effect.last_error_code = "preflight_read_failed"
            effect.lease_owner = None
            effect.lease_expires_at = None
            exhausted = effect.read_attempt_count >= effect.max_read_attempts
            action.write_back_state = (
                BookingWriteBackState.REJECTED
                if exhausted
                else BookingWriteBackState.PENDING
            )
            if exhausted:
                effect.state = ExternalEffectState.DEAD_LETTER
                effect.provider_status = "not_dispatched"
                effect.completed_at = now
                created = _ensure_handoff(
                    session,
                    effect,
                    "preflight_read_exhausted",
                    now,
                )
            else:
                effect.state = ExternalEffectState.PENDING
                available_at = now + timedelta(seconds=30)
                if not_before is not None and not_before > available_at:
                    available_at = not_before
                effect.available_at = available_at
                created = False
        session.commit()
        return exhausted, created


def _settle_rate_limit(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
    reset_at: datetime,
) -> tuple[bool, bool]:
    with session_factory() as session:
        effect, handoff = mark_retryable_failure(
            session,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id=worker_id,
            now=now,
            reason_code="rate_limited",
            not_before=reset_at,
            failure_class="ProviderRateLimited",
        )
        with clinic_scope(session, clinic_id):
            action = session.execute(
                tenant_select(BookingAction).where(BookingAction.id == effect.aggregate_id)
            ).scalar_one()
            exhausted = effect.state == ExternalEffectState.DEAD_LETTER
            action.write_back_state = (
                BookingWriteBackState.REJECTED
                if exhausted
                else BookingWriteBackState.PENDING
            )
        session.commit()
        return exhausted, handoff


def _settle_rejected(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
) -> bool:
    with session_factory() as session:
        effect = mark_rejected(
            session,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id=worker_id,
            now=now,
            reason_code="provider_rejected",
        )
        with clinic_scope(session, clinic_id):
            action = session.execute(
                tenant_select(BookingAction).where(BookingAction.id == effect.aggregate_id)
            ).scalar_one()
            action.write_back_state = BookingWriteBackState.REJECTED
            created = _ensure_handoff(session, effect, "provider_rejected", now)
        session.commit()
        return created


def _settle_ambiguity(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
    reason_code: str = "provider_outcome_unknown",
) -> None:
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            effect = session.execute(
                tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
            ).scalar_one()
            action = session.execute(
                tenant_select(BookingAction).where(BookingAction.id == effect.aggregate_id)
            ).scalar_one()
        if effect.state == ExternalEffectState.DISPATCHING:
            mark_reconcile_required(
                session,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                reason_code=reason_code,
            )
        action.write_back_state = BookingWriteBackState.RECONCILE_REQUIRED
        session.commit()


def _persist_accepted_identity(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    provider_id: str,
) -> None:
    """Persist an accepted resource identity without treating it as verification."""
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            statement = tenant_select(ExternalEffect).where(
                ExternalEffect.id == effect_id
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update()
            effect = session.execute(statement).scalar_one()
            if (
                effect.state != ExternalEffectState.DISPATCHING
                or effect.lease_owner != worker_id
            ):
                raise ValueError("dispatch_identity_state_changed")
            if effect.provider_resource_id not in {None, provider_id}:
                raise ValueError("provider_identity_conflict")
            effect.provider_resource_id = provider_id
            effect.provider_status = "accepted_unverified"
        session.commit()


def _cancel_leased(
    session_factory: SessionFactory,
    clinic_id: str,
    effect_id: str,
    reason_code: str,
) -> None:
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            effect = session.execute(
                tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
            ).scalar_one()
            action = session.execute(
                tenant_select(BookingAction).where(BookingAction.id == effect.aggregate_id)
            ).scalar_one()
            if effect.state in {ExternalEffectState.LEASED, ExternalEffectState.PENDING}:
                effect.state = ExternalEffectState.CANCELED
                effect.provider_status = "not_dispatched"
                effect.last_error_class = "DispatchCanceled"
                effect.last_error_code = reason_code
                effect.lease_owner = None
                effect.lease_expires_at = None
                action.write_back_state = BookingWriteBackState.REJECTED
        session.commit()


def _ensure_handoff(
    session: Session,
    effect: ExternalEffect,
    reason_code: str,
    now: datetime,
) -> bool:
    from ..handoffs import ensure_external_effect_handoff

    _handoff, created = ensure_external_effect_handoff(
        session,
        effect,
        reason_code=reason_code,
        now=now,
    )
    return created


def _project_expired_dispatches(session: Session, clinic_id: str) -> int:
    """Mirror generic dispatch-lease recovery onto booking provider state."""
    with clinic_scope(session, clinic_id):
        rows = list(
            session.execute(
                tenant_select(BookingAction)
                .join(
                    ExternalEffect,
                    ExternalEffect.aggregate_id == BookingAction.id,
                )
                .where(
                    ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING,
                    ExternalEffect.state == ExternalEffectState.RECONCILE_REQUIRED,
                    BookingAction.write_back_state == BookingWriteBackState.DISPATCHING,
                )
            ).scalars()
        )
        for action in rows:
            action.write_back_state = BookingWriteBackState.RECONCILE_REQUIRED
        session.flush()
        return len(rows)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one finite default-off Cliniko booking-write batch."""
    parser = argparse.ArgumentParser(
        description="Run one durable Clinic Recall Cliniko booking-write batch."
    )
    parser.add_argument("--clinic-id", required=True, help="Internal clinic scope.")
    parser.add_argument("--worker-id", default=None, help="Lease owner.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum effects.")
    parser.add_argument("--now", default=None, help="Timezone-aware ISO-8601 time.")
    args = parser.parse_args(argv)

    now = _parse_now(args.now).astimezone(UTC)
    _bootstrap_runtime_configuration()
    if not durable_cliniko_write_enabled():
        print(json.dumps({"claimed": 0, "enabled": False}, sort_keys=True))
        return 0
    switches = operational_switch_snapshot_from_environment()
    if not switches.decision(Channel.CALL, now).allowed:
        print(json.dumps({"claimed": 0, "enabled": False}, sort_keys=True))
        return 0
    try:
        config = get_cliniko_config()
        with httpx.Client() as transport:
            result = run_once(
                get_sessionmaker(),
                clinic_id=args.clinic_id,
                worker_id=args.worker_id or _default_worker_id(),
                client=ClinikoBookingClient(config, client=transport),
                programme_gate=job_gate_for_snapshot(switches, Channel.CALL),
                now=now,
                enabled=True,
                confirmation_release_enabled=durable_booking_confirmation_enabled(),
                limit=args.limit,
            )
    except (ClinikoConfigurationError, ValueError):
        print(
            json.dumps(
                {"claimed": 0, "configuration_blocked": 1, "enabled": False},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result.as_summary(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())