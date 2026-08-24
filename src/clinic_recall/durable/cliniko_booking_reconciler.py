"""Finite read-only reconciliation for ambiguous Cliniko create effects."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import sqlalchemy as sa
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
from ..models import BookingAction, ExternalEffect, ExternalEffectHandoff
from ..pilot_controls import (
    JobPilotGate,
    job_gate_for_snapshot,
    operational_switch_snapshot_from_environment,
)
from ..sync.cliniko_booking import ClinikoBookingClient
from ..sync.cliniko_client import ClinikoRateLimitedError
from ..telemetry import emit_worker_summary
from .cliniko_booking_state import (
    finalize_verified,
    load_dispatch_context,
    preflight_zero_match_hash,
)
from .config import (
    cliniko_booking_reconciliation_enabled,
    durable_booking_confirmation_enabled,
)
from .worker import (
    _bootstrap_runtime_configuration,
    _default_worker_id,
    _parse_now,
)

SessionFactory = Callable[[], Session]


@dataclass(frozen=True)
class ClinikoBookingReconcileResult:
    enabled: bool
    claimed: int = 0
    verified: int = 0
    unresolved: int = 0
    conflicts: int = 0
    exhausted: int = 0
    handoffs_queued: int = 0

    def as_summary(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "claimed": self.claimed,
            "verified": self.verified,
            "unresolved": self.unresolved,
            "conflicts": self.conflicts,
            "exhausted": self.exhausted,
            "handoffs_queued": self.handoffs_queued,
        }


def reconcile_once(
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
) -> ClinikoBookingReconcileResult:
    """Read ambiguous creates; never invoke POST, PATCH, or DELETE."""
    if not enabled:
        return ClinikoBookingReconcileResult(enabled=False)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    now = now.astimezone(UTC)
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            session.execute(
                sa.update(ExternalEffect)
                .where(
                    ExternalEffect.clinic_id == clinic_id,
                    ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING,
                    ExternalEffect.state == ExternalEffectState.RECONCILE_REQUIRED,
                    ExternalEffect.lease_owner.is_not(None),
                    ExternalEffect.lease_expires_at.is_not(None),
                    ExternalEffect.lease_expires_at <= now,
                )
                .values(lease_owner=None, lease_expires_at=None)
            )
            statement = (
                tenant_select(ExternalEffect)
                .where(
                    ExternalEffect.effect_type == ExternalEffectType.CLINIKO_BOOKING,
                    ExternalEffect.state == ExternalEffectState.RECONCILE_REQUIRED,
                    ExternalEffect.available_at <= now,
                    ExternalEffect.lease_owner.is_(None),
                    ExternalEffect.read_attempt_count < ExternalEffect.max_read_attempts,
                    ~sa.exists(
                        sa.select(1).where(
                            ExternalEffectHandoff.clinic_id
                            == ExternalEffect.clinic_id,
                            ExternalEffectHandoff.external_effect_id
                            == ExternalEffect.id,
                        )
                    ),
                    sa.exists(
                        sa.select(1).where(
                            BookingAction.clinic_id == ExternalEffect.clinic_id,
                            BookingAction.id == ExternalEffect.aggregate_id,
                            BookingAction.write_back_state
                            == BookingWriteBackState.RECONCILE_REQUIRED,
                        )
                    ),
                )
                .order_by(ExternalEffect.available_at, ExternalEffect.id)
                .limit(limit)
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            effects = list(session.execute(statement).scalars())
            effect_ids = [effect.id for effect in effects]
            for effect in effects:
                effect.lease_owner = worker_id
                effect.lease_expires_at = now + lease_for
                effect.read_attempt_count += 1
                if effect.settle_deadline_at is None:
                    dispatched_at = effect.dispatch_started_at or now
                    effect.settle_deadline_at = _database_utc(dispatched_at) + timedelta(
                        minutes=5
                    )
            session.commit()

    verified = 0
    unresolved = 0
    conflicts = 0
    exhausted = 0
    handoffs_queued = 0
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
                provider_id, preflight_evidence_valid = _reconciliation_evidence(
                    session,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                )
            if provider_id is not None:
                observed = client.get_individual_appointment(provider_id)
                if not observed.matches(context.expected):
                    if _settle_known_id_conflict(
                        session_factory,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        now=now,
                    ):
                        handoffs_queued += 1
                    conflicts += 1
                    continue
                matches = (observed,)
            else:
                if not preflight_evidence_valid:
                    if _settle_preflight_evidence_conflict(
                        session_factory,
                        clinic_id=clinic_id,
                        effect_id=effect_id,
                        now=now,
                    ):
                        handoffs_queued += 1
                    conflicts += 1
                    continue
                matches = tuple(
                    candidate
                    for candidate in client.list_signature_candidates(context.expected)
                    if candidate.matches(context.expected)
                )
            if len(matches) == 0:
                outcome, handoff = _release_unresolved(
                    session_factory,
                    clinic_id,
                    effect_id,
                    now,
                )
                if outcome == "exhausted":
                    exhausted += 1
                else:
                    unresolved += 1
                handoffs_queued += int(handoff)
                continue
            if len(matches) > 1:
                if _settle_multiple_match(
                    session_factory,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    now=now,
                ):
                    handoffs_queued += 1
                conflicts += 1
                continue
            with session_factory() as session:
                if finalize_verified(
                    session,
                    clinic_id=clinic_id,
                    context=context,
                    observed=matches[0],
                    now=now,
                    programme_gate=programme_gate,
                    confirmation_release_enabled=confirmation_release_enabled,
                    identity_service=identity_service,
                ):
                    verified += 1
                session.commit()
        except ClinikoRateLimitedError as error:
            outcome, handoff = _release_unresolved(
                session_factory,
                clinic_id,
                effect_id,
                now,
                not_before=error.reset_at,
            )
            if outcome == "exhausted":
                exhausted += 1
            else:
                unresolved += 1
            handoffs_queued += int(handoff)
        except Exception:  # noqa: BLE001 - failed reads never authorize replay
            outcome, handoff = _release_unresolved(
                session_factory,
                clinic_id,
                effect_id,
                now,
            )
            if outcome == "exhausted":
                exhausted += 1
            else:
                unresolved += 1
            handoffs_queued += int(handoff)
    result = ClinikoBookingReconcileResult(
        enabled=True,
        claimed=len(effect_ids),
        verified=verified,
        unresolved=unresolved,
        conflicts=conflicts,
        exhausted=exhausted,
        handoffs_queued=handoffs_queued,
    )
    emit_worker_summary("cliniko_reconcile", result.as_summary())
    return result


def _release_unresolved(
    session_factory: SessionFactory,
    clinic_id: str,
    effect_id: str,
    now: datetime,
    *,
    not_before: datetime | None = None,
) -> tuple[str, bool]:
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            effect = session.execute(
                tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
            ).scalar_one()
            effect.lease_owner = None
            effect.lease_expires_at = None
            deadline = _database_utc(effect.settle_deadline_at or now)
            exhausted = (
                effect.read_attempt_count >= effect.max_read_attempts
                or now >= deadline
            )
            if exhausted:
                effect.provider_status = "reconciliation_exhausted"
                effect.last_error_class = "ReconciliationExhausted"
                effect.last_error_code = "exact_match_unresolved"
                handoff = _ensure_handoff(
                    session,
                    effect,
                    "reconciliation_exhausted",
                    now,
                )
                outcome = "exhausted"
            else:
                dispatched_at = _database_utc(effect.dispatch_started_at or now)
                offsets = {1: 30, 2: 120, 3: 300}
                available_at = dispatched_at + timedelta(
                    seconds=offsets[effect.read_attempt_count]
                )
                if not_before is not None and not_before > available_at:
                    available_at = not_before
                effect.available_at = available_at
                effect.last_error_class = "ReconciliationPending"
                effect.last_error_code = "exact_match_unresolved"
                handoff = False
                outcome = "unresolved"
        session.commit()
        return outcome, handoff


def _settle_multiple_match(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    now: datetime,
) -> bool:
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            effect = session.execute(
                tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
            ).scalar_one()
            action = session.execute(
                tenant_select(BookingAction).where(BookingAction.id == effect.aggregate_id)
            ).scalar_one()
            effect.provider_status = "reconciliation_conflict"
            effect.last_error_class = "ReconciliationConflict"
            effect.last_error_code = "multiple_exact_matches"
            effect.lease_owner = None
            effect.lease_expires_at = None
            action.write_back_state = BookingWriteBackState.CONFLICT
            action.conflict_reason = "multiple_exact_matches"
            created = _ensure_handoff(
                session,
                effect,
                "multiple_exact_matches",
                now,
            )
        session.commit()
        return created


def _settle_known_id_conflict(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    now: datetime,
) -> bool:
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            effect = session.execute(
                tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
            ).scalar_one()
            action = session.execute(
                tenant_select(BookingAction).where(BookingAction.id == effect.aggregate_id)
            ).scalar_one()
            effect.provider_status = "reconciliation_conflict"
            effect.last_error_class = "ReconciliationConflict"
            effect.last_error_code = "known_id_state_mismatch"
            effect.lease_owner = None
            effect.lease_expires_at = None
            action.write_back_state = BookingWriteBackState.CONFLICT
            action.conflict_reason = "known_id_state_mismatch"
            created = _ensure_handoff(
                session,
                effect,
                "known_id_state_mismatch",
                now,
            )
        session.commit()
        return created


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


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _reconciliation_evidence(
    session: Session,
    *,
    clinic_id: str,
    effect_id: str,
) -> tuple[str | None, bool]:
    with clinic_scope(session, clinic_id):
        effect = session.execute(
            tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
        ).scalar_one()
        return (
            effect.provider_resource_id,
            effect.preflight_evidence_hash
            == preflight_zero_match_hash(effect.request_hash),
        )


def _settle_preflight_evidence_conflict(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    now: datetime,
) -> bool:
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            effect = session.execute(
                tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
            ).scalar_one()
            action = session.execute(
                tenant_select(BookingAction).where(BookingAction.id == effect.aggregate_id)
            ).scalar_one()
            effect.provider_status = "reconciliation_conflict"
            effect.last_error_class = "ReconciliationConflict"
            effect.last_error_code = "preflight_evidence_missing"
            effect.lease_owner = None
            effect.lease_expires_at = None
            action.write_back_state = BookingWriteBackState.CONFLICT
            action.conflict_reason = "preflight_evidence_missing"
            created = _ensure_handoff(
                session,
                effect,
                "preflight_evidence_missing",
                now,
            )
        session.commit()
        return created


def main(argv: Sequence[str] | None = None) -> int:
    """Run one finite default-off Cliniko read-only reconciliation batch."""
    parser = argparse.ArgumentParser(
        description="Run one Clinic Recall Cliniko booking reconciliation batch."
    )
    parser.add_argument("--clinic-id", required=True, help="Internal clinic scope.")
    parser.add_argument("--worker-id", default=None, help="Lease owner.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum effects.")
    parser.add_argument("--now", default=None, help="Timezone-aware ISO-8601 time.")
    args = parser.parse_args(argv)

    now = _parse_now(args.now).astimezone(UTC)
    _bootstrap_runtime_configuration()
    if not cliniko_booking_reconciliation_enabled():
        print(json.dumps({"claimed": 0, "enabled": False}, sort_keys=True))
        return 0
    switches = operational_switch_snapshot_from_environment()
    if not switches.decision(Channel.CALL, now).allowed:
        print(json.dumps({"claimed": 0, "enabled": False}, sort_keys=True))
        return 0
    try:
        config = get_cliniko_config()
        with httpx.Client() as transport:
            result = reconcile_once(
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