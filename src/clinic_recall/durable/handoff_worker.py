"""Finite, default-off worker for operational handoff notifications."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy.orm import Session

from ..db import clinic_scope, tenant_select
from ..enums import (
    ExternalEffectType,
    HandoffDeliveryState,
    HandoffDestinationRole,
    HandoffRouteKind,
)
from ..handoffs import (
    handoff_owner_is_active,
    pause_clinic_programmes_for_handoff,
    request_alternate_notification,
)
from ..models import ExternalEffect, HandoffReceipt
from .effects import (
    claim_effects,
    lock_dispatching_effect,
    mark_canceled,
    mark_dispatching,
    mark_reconcile_required,
    mark_rejected,
    mark_succeeded,
)

SessionFactory = Callable[[], Session]
_DISPATCH_TOKEN = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_EXPECTED_TEMPLATE = "handoff-v1"


class HandoffNotificationStatus(StrEnum):
    """Structured synchronous outcomes supported by the notifier boundary."""

    ACCEPTED = "accepted"
    PERMANENT_REJECTION = "permanent_rejection"


@dataclass(frozen=True)
class OperationalDestination:
    """Opaque approved route handle; never an address or credential."""

    destination_role: HandoffDestinationRole
    route_kind: HandoffRouteKind
    dispatch_token: str

    def __post_init__(self) -> None:
        if _DISPATCH_TOKEN.fullmatch(self.dispatch_token) is None:
            raise ValueError("operational dispatch token is invalid")


@dataclass(frozen=True)
class HandoffNotificationResult:
    """Bounded provider outcome without raw response or error text."""

    status: HandoffNotificationStatus
    provider_resource_id: str | None = None
    reason_code: str | None = None


class OperationalDestinationResolver(Protocol):
    """Resolve an approved opaque route at dispatch time."""

    def resolve(
        self,
        *,
        destination_role: HandoffDestinationRole,
        route_kind: HandoffRouteKind,
    ) -> OperationalDestination | None: ...


class OperationalHandoffNotifier(Protocol):
    """Send one minimized operational notification to an opaque route."""

    def send(
        self,
        *,
        destination: OperationalDestination,
        receipt_id: str,
        template_version: str,
    ) -> HandoffNotificationResult: ...


class OperationalNotificationOutcomeUnknown(RuntimeError):
    """The provider may have accepted a request; automatic replay is forbidden."""


@dataclass(frozen=True)
class HandoffNotificationRunResult:
    """Aggregate-only result of one bounded worker invocation."""

    enabled: bool
    claimed: int = 0
    sent: int = 0
    rejected: int = 0
    canceled: int = 0
    reconcile_required: int = 0
    destination_unavailable: int = 0
    alternate_requested: int = 0
    programmes_paused: int = 0

    def as_summary(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "claimed": self.claimed,
            "sent": self.sent,
            "rejected": self.rejected,
            "canceled": self.canceled,
            "reconcile_required": self.reconcile_required,
            "destination_unavailable": self.destination_unavailable,
            "alternate_requested": self.alternate_requested,
            "programmes_paused": self.programmes_paused,
        }


@dataclass(frozen=True)
class _PreparedDispatch:
    destination: OperationalDestination
    receipt_id: str


def run_handoff_notifications_once(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    worker_id: str,
    destination_resolver: OperationalDestinationResolver,
    notifier: OperationalHandoffNotifier,
    now: datetime,
    enabled: bool = False,
    lease_for: timedelta = timedelta(minutes=5),
    limit: int = 10,
) -> HandoffNotificationRunResult:
    """Commit a complete claimed batch before performing any provider I/O."""
    if not enabled:
        return HandoffNotificationRunResult(enabled=False)
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
            effect_types=(ExternalEffectType.HANDOFF_NOTIFICATION,),
        )
        effect_ids = [effect.id for effect in claimed]
        for effect_id in effect_ids:
            mark_dispatching(
                session,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
            )
        session.commit()

    counters = {
        "sent": 0,
        "rejected": 0,
        "canceled": 0,
        "reconcile_required": 0,
        "destination_unavailable": 0,
        "alternate_requested": 0,
        "programmes_paused": 0,
    }
    for effect_id in effect_ids:
        prepared = _prepare_dispatch(
            session_factory,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id=worker_id,
            destination_resolver=destination_resolver,
            now=now,
            counters=counters,
        )
        if prepared is None:
            continue
        try:
            outcome = notifier.send(
                destination=prepared.destination,
                receipt_id=prepared.receipt_id,
                template_version=_EXPECTED_TEMPLATE,
            )
        except Exception:
            _settle_ambiguous(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                counters=counters,
            )
            continue
        if (
            not isinstance(outcome, HandoffNotificationResult)
            or outcome.status == HandoffNotificationStatus.ACCEPTED
            and not outcome.provider_resource_id
        ):
            _settle_ambiguous(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                counters=counters,
            )
        elif outcome.status == HandoffNotificationStatus.ACCEPTED:
            _settle_accepted(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                provider_resource_id=outcome.provider_resource_id or "",
            )
            counters["sent"] += 1
        else:
            _settle_permanent_rejection(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                counters=counters,
            )
    for outcome in (
        "destination_unavailable",
        "reconcile_required",
        "sent",
    ):
        count = {
            "destination_unavailable": counters["destination_unavailable"],
            "reconcile_required": counters["reconcile_required"],
            "sent": counters["sent"],
        }[outcome]
        if count:
            with session_factory() as session:
                from ..telemetry import queue_after_commit

                queue_after_commit(
                    session,
                    "handoff.notification.outcome",
                    {"outcome": outcome, "count": count},
                )
                session.commit()
    return HandoffNotificationRunResult(
        enabled=True,
        claimed=len(effect_ids),
        **counters,
    )


def _prepare_dispatch(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    destination_resolver: OperationalDestinationResolver,
    now: datetime,
    counters: dict[str, int],
) -> _PreparedDispatch | None:
    with session_factory() as session:
        effect = lock_dispatching_effect(
            session,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id=worker_id,
        )
        receipt = _load_receipt(session, effect)
        if receipt is None:
            mark_reconcile_required(
                session,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                reason_code="handoff_owner_invariant_broken",
            )
            counters["programmes_paused"] += pause_clinic_programmes_for_handoff(
                session,
                clinic_id=clinic_id,
                now=now,
                reason_code="handoff_owner_invariant_broken",
            )
            session.commit()
            counters["reconcile_required"] += 1
            return None
        if not _effect_contract_valid(effect, receipt):
            mark_reconcile_required(
                session,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                reason_code="invalid_handoff_effect_contract",
            )
            session.commit()
            counters["reconcile_required"] += 1
            return None
        if (
            receipt.acknowledged_at is not None
            or receipt.resolved_at is not None
            or not handoff_owner_is_active(session, receipt)
        ):
            mark_canceled(
                session,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                reason_code="handoff_no_longer_open",
            )
            session.commit()
            counters["canceled"] += 1
            return None
        role = HandoffDestinationRole(str(effect.payload["destination_role"]))
        route = HandoffRouteKind(str(effect.payload["route_kind"]))
        try:
            destination = destination_resolver.resolve(
                destination_role=role,
                route_kind=route,
            )
        except Exception:
            destination = None
        if destination is None or (
            destination.destination_role != role or destination.route_kind != route
        ):
            mark_rejected(
                session,
                clinic_id=clinic_id,
                effect_id=effect_id,
                worker_id=worker_id,
                now=now,
                reason_code="handoff_destination_unavailable",
            )
            if receipt.delivery_state != HandoffDeliveryState.DELIVERED:
                receipt.delivery_state = HandoffDeliveryState.DEFINITIVE_FAILURE
            counters["alternate_requested"] += int(
                request_alternate_notification(
                    session,
                    receipt,
                    now=now,
                    reason_code="handoff_destination_unavailable",
                )
            )
            counters["programmes_paused"] += pause_clinic_programmes_for_handoff(
                session,
                clinic_id=clinic_id,
                now=now,
                reason_code="handoff_destination_unavailable",
            )
            session.commit()
            counters["destination_unavailable"] += 1
            counters["rejected"] += 1
            return None
        session.commit()
        return _PreparedDispatch(destination=destination, receipt_id=receipt.id)


def _settle_accepted(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
    provider_resource_id: str,
) -> None:
    with session_factory() as session:
        effect = lock_dispatching_effect(
            session,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id=worker_id,
        )
        receipt = _load_receipt(session, effect)
        if receipt is None:
            raise RuntimeError("handoff receipt disappeared during dispatch")
        mark_succeeded(
            session,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id=worker_id,
            now=now,
            provider_resource_id=provider_resource_id,
        )
        receipt.sent_at = receipt.sent_at or now
        if receipt.delivery_state != HandoffDeliveryState.DELIVERED:
            receipt.delivery_state = HandoffDeliveryState.SENT
        session.commit()


def _settle_permanent_rejection(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
    counters: dict[str, int],
) -> None:
    with session_factory() as session:
        effect = lock_dispatching_effect(
            session,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id=worker_id,
        )
        receipt = _load_receipt(session, effect)
        if receipt is None:
            raise RuntimeError("handoff receipt disappeared during rejection")
        mark_rejected(
            session,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id=worker_id,
            now=now,
            reason_code="provider_permanent_rejection",
        )
        if receipt.delivery_state != HandoffDeliveryState.DELIVERED:
            receipt.delivery_state = HandoffDeliveryState.DEFINITIVE_FAILURE
        counters["alternate_requested"] += int(
            request_alternate_notification(
                session,
                receipt,
                now=now,
                reason_code="provider_permanent_rejection",
            )
        )
        counters["programmes_paused"] += pause_clinic_programmes_for_handoff(
            session,
            clinic_id=clinic_id,
            now=now,
            reason_code="handoff_destination_unavailable",
        )
        session.commit()
        counters["rejected"] += 1


def _settle_ambiguous(
    session_factory: SessionFactory,
    *,
    clinic_id: str,
    effect_id: str,
    worker_id: str,
    now: datetime,
    counters: dict[str, int],
) -> None:
    with session_factory() as session:
        effect = lock_dispatching_effect(
            session,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id=worker_id,
        )
        receipt = _load_receipt(session, effect)
        mark_reconcile_required(
            session,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id=worker_id,
            now=now,
            reason_code="provider_outcome_unknown",
        )
        if receipt is not None:
            if receipt.delivery_state != HandoffDeliveryState.DELIVERED:
                receipt.delivery_state = HandoffDeliveryState.RECONCILE_REQUIRED
            counters["alternate_requested"] += int(
                request_alternate_notification(
                    session,
                    receipt,
                    now=now,
                    reason_code="provider_outcome_unknown",
                )
            )
        session.commit()
        counters["reconcile_required"] += 1


def _load_receipt(
    session: Session,
    effect: ExternalEffect,
) -> HandoffReceipt | None:
    if (
        effect.effect_type != ExternalEffectType.HANDOFF_NOTIFICATION
        or effect.aggregate_type != "handoff_receipt"
    ):
        return None
    with clinic_scope(session, effect.clinic_id):
        return session.execute(
            tenant_select(HandoffReceipt).where(
                HandoffReceipt.id == effect.aggregate_id
            )
        ).scalar_one_or_none()


def _effect_contract_valid(effect: ExternalEffect, receipt: HandoffReceipt) -> bool:
    expected = {
        "destination_role": HandoffDestinationRole.CLINIC_OPERATIONS.value,
        "receipt_id": receipt.id,
        "route_kind": HandoffRouteKind.OPERATIONAL_EMAIL.value,
        "template_version": _EXPECTED_TEMPLATE,
    }
    return effect.payload_version == 1 and effect.payload == expected