"""Finite durable execution and reconciliation for privacy-rights targets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..config import get_rights_residual_approvals, get_rights_subject_keyring
from ..db import clinic_scope, get_privacy_sessionmaker, tenant_select
from ..enums import (
    AuditAction,
    ExternalEffectState,
    ExternalEffectType,
    RightsRequestKind,
    RightsRequestState,
    RightsResidualCategory,
    RightsTargetAction,
    RightsTargetOwnerType,
    RightsTargetResource,
    RightsTargetState,
    RightsTargetSystem,
)
from ..messaging.audit import audit_action
from ..models import (
    CallRecord,
    ExternalEffect,
    ExternalEffectHandoff,
    InboundCall,
    InboundMessage,
    Interaction,
    RightsRequest,
    RightsTarget,
)
from ..rights import (
    ResidualApproval,
    RightsFinalizationResult,
    RightsResidualMaintenanceResult,
    SubjectKeyring,
    finalize_ready_patient_erasures,
    maintain_residual_targets,
)
from ..rights_adapters import (
    AzureBlobRightsAdapter,
    RightsAdapter,
    RightsAdapterDisposition,
    RightsAdapterReason,
    RightsAdapterResult,
    TwilioRightsAdapter,
)
from ..telemetry import configure_job_telemetry, emit_worker_summary
from .config import (
    durable_rights_blob_enabled,
    durable_rights_enabled,
    durable_rights_twilio_enabled,
)
from .effects import claim_effects, mark_dispatching
from .enqueue import enqueue_rights_effect

SessionFactory = type[Session] | object
AdapterMap = Mapping[RightsTargetSystem, RightsAdapter]


@dataclass(frozen=True)
class RightsRunOnceResult:
    """Aggregate-only result from one bounded destructive worker batch."""

    enabled: bool
    claimed: int = 0
    verified: int = 0
    residual: int = 0
    retried: int = 0
    reconcile_required: int = 0
    configuration_blocked: int = 0

    def as_summary(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "claimed": self.claimed,
            "verified": self.verified,
            "residual": self.residual,
            "retried": self.retried,
            "reconcile_required": self.reconcile_required,
            "configuration_blocked": self.configuration_blocked,
        }


@dataclass(frozen=True)
class RightsReconcileResult:
    """Aggregate-only result from one bounded read-only reconciliation pass."""

    enabled: bool
    inspected: int = 0
    verified: int = 0
    residual: int = 0
    retried: int = 0
    reconcile_required: int = 0
    configuration_blocked: int = 0
    handoffs_queued: int = 0

    def as_summary(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "inspected": self.inspected,
            "verified": self.verified,
            "residual": self.residual,
            "retried": self.retried,
            "reconcile_required": self.reconcile_required,
            "configuration_blocked": self.configuration_blocked,
            "handoffs_queued": self.handoffs_queued,
        }


@dataclass(frozen=True)
class _Locator:
    value: str
    created_at: datetime | None


def run_once(
    session_factory,
    *,
    clinic_id: str,
    worker_id: str,
    adapters: AdapterMap,
    now: datetime,
    enabled: bool = False,
    lease_for: timedelta = timedelta(minutes=5),
    limit: int = 10,
    max_target_attempts: int,
    residual_approvals: Mapping[RightsResidualCategory, ResidualApproval] | None = None,
) -> RightsRunOnceResult:
    """Claim and execute a finite RIGHTS batch with commit-before-provider-I/O."""
    if not enabled:
        return RightsRunOnceResult(enabled=False)
    now = _aware_utc(now, "now")
    _validate_worker_args(clinic_id, worker_id, limit, max_target_attempts)
    approvals = residual_approvals or {}

    with session_factory() as session:
        claimed = claim_effects(
            session,
            clinic_id=clinic_id,
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
            limit=limit,
            effect_types=(ExternalEffectType.RIGHTS,),
        )
        _recover_target_reconciliation_states(session, clinic_id)
        effect_ids = [effect.id for effect in claimed]
        session.commit()

    counters = {
        "verified": 0,
        "residual": 0,
        "retried": 0,
        "reconcile_required": 0,
        "configuration_blocked": 0,
    }
    for effect_id in effect_ids:
        prepared = _prepare_dispatch(
            session_factory,
            clinic_id=clinic_id,
            worker_id=worker_id,
            effect_id=effect_id,
            now=now,
        )
        if isinstance(prepared, str):
            _settle_configuration_blocked(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                now=now,
                reason=prepared,
            )
            counters["configuration_blocked"] += 1
            continue
        target_id, target_system, target_resource, locator = prepared
        if _is_local_interaction_target(target_system, target_resource):
            try:
                category = _execute_local_interaction_target(
                    session_factory,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    target_id=target_id,
                    now=now,
                )
            except Exception:  # noqa: BLE001 - commit outcome may be ambiguous
                category = _settle_local_execution_uncertainty(
                    session_factory,
                    clinic_id=clinic_id,
                    effect_id=effect_id,
                    target_id=target_id,
                    now=now,
                )
            counters[category] += 1
            continue
        adapter = adapters.get(target_system)
        if adapter is None:
            _settle_configuration_blocked(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                now=now,
                reason="adapter_unconfigured",
            )
            counters["configuration_blocked"] += 1
            continue

        try:
            outcome = adapter.delete(
                resource=target_resource,
                locator=locator.value,
                resource_created_at=locator.created_at,
                now=now,
            )
            if outcome.disposition in {
                RightsAdapterDisposition.DELETED,
                RightsAdapterDisposition.ALREADY_ABSENT,
            }:
                outcome = adapter.verify_absent(
                    resource=target_resource,
                    locator=locator.value,
                    dispatched_at=now,
                    now=now,
                )
        except Exception:  # noqa: BLE001 - any post-commit uncertainty is ambiguous
            outcome = RightsAdapterResult(
                RightsAdapterDisposition.AMBIGUOUS,
                RightsAdapterReason.TRANSPORT_ERROR,
            )

        category = _settle_outcome(
            session_factory,
            clinic_id=clinic_id,
            effect_id=effect_id,
            target_id=target_id,
            outcome=outcome,
            now=now,
            max_target_attempts=max_target_attempts,
            approvals=approvals,
        )
        counters[category] += 1

    result = RightsRunOnceResult(
        enabled=True,
        claimed=len(effect_ids),
        **counters,
    )
    emit_worker_summary("rights_dispatch", result.as_summary())
    return result


def reconcile_once(
    session_factory,
    *,
    clinic_id: str,
    worker_id: str,
    adapters: AdapterMap,
    now: datetime,
    enabled: bool = False,
    limit: int = 10,
    max_target_attempts: int,
    residual_approvals: Mapping[RightsResidualCategory, ResidualApproval] | None = None,
    reconcile_lease_for: timedelta = timedelta(minutes=5),
) -> RightsReconcileResult:
    """Inspect ambiguous targets before any explicitly bounded new delete."""
    if not enabled:
        return RightsReconcileResult(enabled=False)
    now = _aware_utc(now, "now")
    _validate_worker_args(clinic_id, worker_id, limit, max_target_attempts)
    if reconcile_lease_for <= timedelta(0):
        raise ValueError("reconcile_lease_for must be positive")
    approvals = residual_approvals or {}

    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            _recover_stale_reconciliation_claims(
                session,
                clinic_id,
                now - reconcile_lease_for,
            )
            statement = (
                tenant_select(RightsTarget)
                .where(
                    RightsTarget.state == RightsTargetState.RECONCILE_REQUIRED,
                    RightsTarget.available_at <= now,
                    ~sa.exists(
                        sa.select(1).where(
                            ExternalEffectHandoff.clinic_id
                            == RightsTarget.clinic_id,
                            ExternalEffectHandoff.external_effect_id
                            == RightsTarget.current_effect_id,
                        )
                    ),
                )
                .order_by(RightsTarget.available_at, RightsTarget.created_at, RightsTarget.id)
                .limit(limit)
            )
            if _is_postgresql(session):
                statement = statement.with_for_update(skip_locked=True)
            targets = list(session.execute(statement).scalars())
            target_ids = [target.id for target in targets]
            for target in targets:
                target.state = RightsTargetState.DISPATCHING
                target.reconciliation_count += 1
                target.last_reconciled_at = now
            session.commit()

    counters = {
        "verified": 0,
        "residual": 0,
        "retried": 0,
        "reconcile_required": 0,
        "configuration_blocked": 0,
        "handoffs_queued": 0,
    }
    for target_id in target_ids:
        resolved = _reconciliation_context(
            session_factory,
            clinic_id=clinic_id,
            target_id=target_id,
        )
        if isinstance(resolved, str):
            _restore_reconcile_state(
                session_factory,
                clinic_id=clinic_id,
                target_id=target_id,
                reason=resolved,
            )
            counters["configuration_blocked"] += 1
            continue
        effect_id, target_system, target_resource, locator, dispatched_at = resolved
        if _is_local_interaction_target(target_system, target_resource):
            category = _reconcile_local_interaction_target(
                session_factory,
                clinic_id=clinic_id,
                effect_id=effect_id,
                target_id=target_id,
                now=now,
                max_target_attempts=max_target_attempts,
            )
            counters[category] += 1
            continue
        adapter = adapters.get(target_system)
        if adapter is None:
            _restore_reconcile_state(
                session_factory,
                clinic_id=clinic_id,
                target_id=target_id,
                reason="adapter_unconfigured",
            )
            counters["configuration_blocked"] += 1
            continue
        try:
            outcome = adapter.verify_absent(
                resource=target_resource,
                locator=locator.value,
                dispatched_at=dispatched_at,
                now=now,
            )
        except Exception:  # noqa: BLE001 - a failed check never authorizes replay
            outcome = RightsAdapterResult(
                RightsAdapterDisposition.AMBIGUOUS,
                RightsAdapterReason.TRANSPORT_ERROR,
            )
        category = _settle_outcome(
            session_factory,
            clinic_id=clinic_id,
            effect_id=effect_id,
            target_id=target_id,
            outcome=outcome,
            now=now,
            max_target_attempts=max_target_attempts,
            approvals=approvals,
            reconciling=True,
        )
        if category == "reconciliation_exhausted":
            counters["reconcile_required"] += 1
            counters["handoffs_queued"] += 1
        else:
            counters[category] += 1

    result = RightsReconcileResult(
        enabled=True,
        inspected=len(target_ids),
        **counters,
    )
    emit_worker_summary("rights_reconcile", result.as_summary())
    return result


def _prepare_dispatch(
    session_factory,
    *,
    clinic_id: str,
    worker_id: str,
    effect_id: str,
    now: datetime,
) -> tuple[
    str,
    RightsTargetSystem,
    RightsTargetResource,
    _Locator,
] | str:
    with session_factory() as session:
        effect = mark_dispatching(
            session,
            clinic_id=clinic_id,
            effect_id=effect_id,
            worker_id=worker_id,
            now=now,
        )
        target = _target_for_effect(session, clinic_id, effect)
        if target is None:
            session.rollback()
            return "target_contract_invalid"
        locator = (
            _Locator(target.owner_id, None)
            if _is_local_interaction_target(target.system, target.resource)
            else _resolve_locator(session, clinic_id, target)
        )
        if locator is None:
            target.state = RightsTargetState.RECONCILE_REQUIRED
            target.reason_code = "locator_missing"
            session.commit()
            return "locator_missing"
        target.state = RightsTargetState.DISPATCHING
        target.disposition_code = None
        target.reason_code = None
        session.commit()
        return target.id, target.system, target.resource, locator


def _reconciliation_context(
    session_factory,
    *,
    clinic_id: str,
    target_id: str,
) -> tuple[
    str,
    RightsTargetSystem,
    RightsTargetResource,
    _Locator,
    datetime,
] | str:
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            target = session.execute(
                tenant_select(RightsTarget).where(RightsTarget.id == target_id)
            ).scalar_one_or_none()
            if target is None or not target.current_effect_id:
                return "target_contract_invalid"
            effect = session.execute(
                tenant_select(ExternalEffect).where(
                    ExternalEffect.id == target.current_effect_id
                )
            ).scalar_one_or_none()
            if effect is None or effect.state != ExternalEffectState.RECONCILE_REQUIRED:
                return "effect_contract_invalid"
            locator = (
                _Locator(target.owner_id, None)
                if _is_local_interaction_target(target.system, target.resource)
                else _resolve_locator(session, clinic_id, target)
            )
            if locator is None:
                return "locator_missing"
            dispatched_at = effect.dispatch_started_at or effect.updated_at
            return (
                effect.id,
                target.system,
                target.resource,
                locator,
                _database_utc(dispatched_at),
            )


def _execute_local_interaction_target(
    session_factory,
    *,
    clinic_id: str,
    effect_id: str,
    target_id: str,
    now: datetime,
) -> str:
    with session_factory() as session:
        effect, target = _lock_effect_target(
            session,
            clinic_id,
            effect_id,
            target_id,
        )
        if not _valid_local_interaction_contract(target):
            _finish_effect(
                effect,
                ExternalEffectState.REJECTED,
                now,
                "configuration_blocked",
                "local_target_contract_invalid",
            )
            target.state = RightsTargetState.RECONCILE_REQUIRED
            target.disposition_code = (
                RightsAdapterDisposition.CONFIGURATION_BLOCKED.value
            )
            target.reason_code = "local_target_contract_invalid"
            session.commit()
            return "configuration_blocked"
        interaction = _lock_interaction_owner(session, clinic_id, target.owner_id)
        was_present = interaction is not None and interaction.content is not None
        if was_present:
            interaction.content = None
        _settle_local_verified(
            session,
            effect=effect,
            target=target,
            now=now,
            reason=(
                "local_content_minimized"
                if was_present
                else "local_content_already_absent"
            ),
        )
        session.commit()
        return "verified"


def _settle_local_execution_uncertainty(
    session_factory,
    *,
    clinic_id: str,
    effect_id: str,
    target_id: str,
    now: datetime,
) -> str:
    with session_factory() as session:
        effect, target = _lock_effect_target(
            session,
            clinic_id,
            effect_id,
            target_id,
        )
        if (
            effect.state == ExternalEffectState.SUCCEEDED
            and target.state == RightsTargetState.VERIFIED
        ):
            return "verified"
        _settle_ambiguous(effect, target, now, "local_commit_outcome_unknown")
        session.commit()
        return "reconcile_required"


def _reconcile_local_interaction_target(
    session_factory,
    *,
    clinic_id: str,
    effect_id: str,
    target_id: str,
    now: datetime,
    max_target_attempts: int,
) -> str:
    with session_factory() as session:
        effect, target = _lock_effect_target(
            session,
            clinic_id,
            effect_id,
            target_id,
        )
        if not _valid_local_interaction_contract(target):
            target.state = RightsTargetState.RECONCILE_REQUIRED
            target.disposition_code = (
                RightsAdapterDisposition.CONFIGURATION_BLOCKED.value
            )
            target.reason_code = "local_target_contract_invalid"
            session.commit()
            return "configuration_blocked"
        interaction = _lock_interaction_owner(session, clinic_id, target.owner_id)
        if interaction is None or interaction.content is None:
            _settle_local_verified(
                session,
                effect=effect,
                target=target,
                now=now,
                reason="local_content_already_absent",
            )
            session.commit()
            return "verified"

        target.disposition_code = (
            RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE.value
        )
        target.reason_code = RightsAdapterReason.RESOURCE_PRESENT.value
        if target.attempt_ordinal < max_target_attempts:
            _finish_effect(
                effect,
                ExternalEffectState.REJECTED,
                now,
                "known_not_deleted",
                RightsAdapterReason.RESOURCE_PRESENT.value,
            )
            target.state = RightsTargetState.REQUESTED
            target.attempt_ordinal += 1
            target.available_at = now
            next_effect, _ = enqueue_rights_effect(
                session,
                clinic_id=clinic_id,
                target_id=target.id,
                attempt_ordinal=target.attempt_ordinal,
                available_at=now,
            )
            target.current_effect_id = next_effect.id
            session.commit()
            return "retried"
        _settle_attempt_limit_reconciliation(effect, target, now)
        target.reason_code = "attempt_limit_reached"
        session.commit()
        return "reconcile_required"


def _lock_interaction_owner(
    session: Session,
    clinic_id: str,
    interaction_id: str,
) -> Interaction | None:
    with clinic_scope(session, clinic_id):
        statement = tenant_select(Interaction).where(Interaction.id == interaction_id)
        if _is_postgresql(session):
            statement = statement.with_for_update()
        return session.execute(statement).scalar_one_or_none()


def _settle_local_verified(
    session: Session,
    *,
    effect: ExternalEffect,
    target: RightsTarget,
    now: datetime,
    reason: str,
) -> None:
    target.state = RightsTargetState.VERIFIED
    target.verified_at = target.verified_at or now
    target.locator_cleared_at = target.locator_cleared_at or now
    target.disposition_code = RightsAdapterDisposition.ALREADY_ABSENT.value
    target.reason_code = reason
    _clear_residual_approval(target)
    _finish_effect(
        effect,
        ExternalEffectState.SUCCEEDED,
        now,
        "verified_absent",
        reason,
    )
    _complete_local_retention_request(session, target=target, now=now)


def _complete_local_retention_request(
    session: Session,
    *,
    target: RightsTarget,
    now: datetime,
) -> None:
    with clinic_scope(session, target.clinic_id):
        request_statement = tenant_select(RightsRequest).where(
            RightsRequest.id == target.request_id
        )
        targets_statement = tenant_select(RightsTarget).where(
            RightsTarget.request_id == target.request_id
        )
        if _is_postgresql(session):
            request_statement = request_statement.with_for_update()
            targets_statement = targets_statement.with_for_update()
        request = session.execute(request_statement).scalar_one()
        targets = list(session.execute(targets_statement).scalars())
        if request.kind != RightsRequestKind.RETENTION:
            raise RuntimeError("local target request kind is not retention")
        request.target_count = len(targets)
        request.verified_target_count = sum(
            item.state == RightsTargetState.VERIFIED for item in targets
        )
        request.residual_target_count = sum(
            item.state == RightsTargetState.RESIDUAL for item in targets
        )
        if request.verified_target_count != len(targets):
            request.state = RightsRequestState.VERIFYING
            request.verifying_at = request.verifying_at or now
            return
        was_completed = request.state == RightsRequestState.COMPLETED
        request.state = RightsRequestState.COMPLETED
        request.deleting_at = request.deleting_at or now
        request.verifying_at = request.verifying_at or now
        request.completed_at = request.completed_at or now
        request.completion_evidence_hash = _request_completion_evidence_hash(
            request,
            targets,
        )
        if not was_completed:
            audit_action(
                session,
                request.clinic_id,
                AuditAction.RETENTION_PURGE,
                request.id,
                {
                    "request_id": request.id,
                    "state": request.state.value,
                    "target_count": request.target_count,
                    "verified_target_count": request.verified_target_count,
                    "occurred_at": now,
                },
                actor="system:retention",
            )


def _request_completion_evidence_hash(
    request: RightsRequest,
    targets: list[RightsTarget],
) -> str:
    encoded = json.dumps(
        {
            "request_id": request.id,
            "state": RightsRequestState.COMPLETED.value,
            "targets": [
                {
                    "state": target.state.value,
                    "target_key_hash": target.target_key_hash,
                }
                for target in sorted(targets, key=lambda item: item.target_key_hash)
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_local_interaction_target(
    system: RightsTargetSystem,
    resource: RightsTargetResource,
) -> bool:
    return (
        system == RightsTargetSystem.LOCAL
        and resource == RightsTargetResource.INTERACTION_CONTENT
    )


def _valid_local_interaction_contract(target: RightsTarget) -> bool:
    return (
        _is_local_interaction_target(target.system, target.resource)
        and target.action == RightsTargetAction.MINIMIZE
        and target.owner_type == RightsTargetOwnerType.INTERACTION
    )


def _settle_outcome(
    session_factory,
    *,
    clinic_id: str,
    effect_id: str,
    target_id: str,
    outcome: RightsAdapterResult,
    now: datetime,
    max_target_attempts: int,
    approvals: Mapping[RightsResidualCategory, ResidualApproval],
    reconciling: bool = False,
) -> str:
    with session_factory() as session:
        effect, target = _lock_effect_target(
            session,
            clinic_id,
            effect_id,
            target_id,
        )
        target.disposition_code = outcome.disposition.value
        target.reason_code = outcome.reason.value
        if outcome.disposition in {
            RightsAdapterDisposition.DELETED,
            RightsAdapterDisposition.ALREADY_ABSENT,
        }:
            _settle_verified(effect, target, now)
            session.commit()
            return "verified"
        if outcome.disposition == RightsAdapterDisposition.RESIDUAL:
            settled = _settle_residual(effect, target, outcome, approvals, now)
            session.commit()
            return "residual" if settled else "reconcile_required"
        if outcome.disposition == RightsAdapterDisposition.RETRYABLE_KNOWN_FAILURE:
            if outcome.reason == RightsAdapterReason.RESOURCE_PRESENT and not reconciling:
                _settle_ambiguous(effect, target, now, outcome.reason.value)
                session.commit()
                return "reconcile_required"
            retry_at = outcome.retry_at or now
            if target.attempt_ordinal < max_target_attempts:
                _finish_effect(
                    effect,
                    ExternalEffectState.REJECTED,
                    now,
                    "known_not_deleted",
                    outcome.reason.value,
                )
                target.state = RightsTargetState.REQUESTED
                target.attempt_ordinal += 1
                target.available_at = retry_at
                next_effect, _ = enqueue_rights_effect(
                    session,
                    clinic_id=clinic_id,
                    target_id=target.id,
                    attempt_ordinal=target.attempt_ordinal,
                    available_at=retry_at,
                )
                target.current_effect_id = next_effect.id
                session.commit()
                return "retried"
            if reconciling and target.reconciliation_count >= max_target_attempts:
                created = _settle_reconciliation_exhausted(
                    session,
                    effect,
                    target,
                    now,
                )
                session.commit()
                return (
                    "reconciliation_exhausted"
                    if created
                    else "reconcile_required"
                )
            _settle_attempt_limit_reconciliation(effect, target, now)
            target.reason_code = "attempt_limit_reached"
            session.commit()
            return "reconcile_required"
        if outcome.disposition == RightsAdapterDisposition.AMBIGUOUS:
            if reconciling and target.reconciliation_count >= max_target_attempts:
                created = _settle_reconciliation_exhausted(
                    session,
                    effect,
                    target,
                    now,
                )
                session.commit()
                return (
                    "reconciliation_exhausted"
                    if created
                    else "reconcile_required"
                )
            _settle_ambiguous(effect, target, now, outcome.reason.value)
            session.commit()
            return "reconcile_required"

        _settle_configuration_reconciliation(
            effect,
            target,
            now,
            outcome.reason.value,
        )
        session.commit()
        return "configuration_blocked"


def _settle_configuration_blocked(
    session_factory,
    *,
    clinic_id: str,
    effect_id: str,
    now: datetime,
    reason: str,
) -> None:
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            effect = session.execute(
                tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
            ).scalar_one_or_none()
            target = (
                session.execute(
                    tenant_select(RightsTarget).where(
                        RightsTarget.current_effect_id == effect_id
                    )
                ).scalar_one_or_none()
                if effect is not None
                else None
            )
            if effect is not None:
                if reason == "target_contract_invalid":
                    _finish_effect(
                        effect,
                        ExternalEffectState.REJECTED,
                        now,
                        "configuration_blocked",
                        reason,
                    )
                elif target is not None:
                    _settle_configuration_reconciliation(
                        effect,
                        target,
                        now,
                        reason,
                    )
            if target is not None:
                target.state = RightsTargetState.RECONCILE_REQUIRED
                target.disposition_code = RightsAdapterDisposition.CONFIGURATION_BLOCKED.value
                target.reason_code = reason
            session.commit()


def _settle_configuration_reconciliation(
    effect: ExternalEffect,
    target: RightsTarget,
    now: datetime,
    reason: str,
) -> None:
    effect.state = ExternalEffectState.RECONCILE_REQUIRED
    effect.provider_status = "configuration_blocked"
    effect.last_error_class = "RightsConfigurationError"
    effect.last_error_code = reason
    effect.completed_at = None
    effect.completion_evidence_hash = None
    effect.lease_owner = None
    effect.lease_expires_at = None
    target.state = RightsTargetState.RECONCILE_REQUIRED
    target.available_at = now


def _settle_attempt_limit_reconciliation(
    effect: ExternalEffect,
    target: RightsTarget,
    now: datetime,
) -> None:
    effect.state = ExternalEffectState.RECONCILE_REQUIRED
    effect.provider_status = "attempt_limit_reached"
    effect.last_error_class = "RightsEffect"
    effect.last_error_code = "attempt_limit_reached"
    effect.completed_at = None
    effect.completion_evidence_hash = None
    effect.lease_owner = None
    effect.lease_expires_at = None
    target.state = RightsTargetState.RECONCILE_REQUIRED
    target.available_at = now


def _settle_reconciliation_exhausted(
    session: Session,
    effect: ExternalEffect,
    target: RightsTarget,
    now: datetime,
) -> bool:
    effect.state = ExternalEffectState.RECONCILE_REQUIRED
    effect.provider_status = "reconciliation_exhausted"
    effect.last_error_class = "RightsReconciliationError"
    effect.last_error_code = "reconciliation_exhausted"
    effect.completed_at = None
    effect.completion_evidence_hash = None
    effect.lease_owner = None
    effect.lease_expires_at = None
    target.state = RightsTargetState.RECONCILE_REQUIRED
    target.reason_code = "reconciliation_exhausted"
    target.available_at = now
    from ..handoffs import ensure_external_effect_handoff

    _handoff, created = ensure_external_effect_handoff(
        session,
        effect,
        reason_code="reconciliation_exhausted",
        now=now,
    )
    return created


def _restore_reconcile_state(
    session_factory,
    *,
    clinic_id: str,
    target_id: str,
    reason: str,
) -> None:
    with session_factory() as session:
        with clinic_scope(session, clinic_id):
            target = session.execute(
                tenant_select(RightsTarget).where(RightsTarget.id == target_id)
            ).scalar_one_or_none()
            if target is not None:
                target.state = RightsTargetState.RECONCILE_REQUIRED
                target.reason_code = reason
            session.commit()


def _target_for_effect(
    session: Session,
    clinic_id: str,
    effect: ExternalEffect,
) -> RightsTarget | None:
    payload = effect.payload if isinstance(effect.payload, dict) else {}
    attempt_ordinal = payload.get("attempt_ordinal")
    if (
        effect.effect_type != ExternalEffectType.RIGHTS
        or effect.aggregate_type != "rights_target"
        or effect.payload_version != 1
        or effect.max_attempts != 1
        or effect.attempt_count != 1
        or not isinstance(attempt_ordinal, int)
        or attempt_ordinal < 1
        or payload
        != {
            "intent": "rights_target_execute",
            "target_id": effect.aggregate_id,
            "attempt_ordinal": attempt_ordinal,
        }
    ):
        return None
    with clinic_scope(session, clinic_id):
        statement = tenant_select(RightsTarget).where(
            RightsTarget.id == effect.aggregate_id,
            RightsTarget.current_effect_id == effect.id,
            RightsTarget.attempt_ordinal == attempt_ordinal,
        )
        if _is_postgresql(session):
            statement = statement.with_for_update()
        return session.execute(statement).scalar_one_or_none()


def _resolve_locator(
    session: Session,
    clinic_id: str,
    target: RightsTarget,
) -> _Locator | None:
    with clinic_scope(session, clinic_id):
        if target.owner_type == RightsTargetOwnerType.EXTERNAL_EFFECT:
            owner = session.execute(
                tenant_select(ExternalEffect).where(ExternalEffect.id == target.owner_id)
            ).scalar_one_or_none()
            if owner is None or not owner.provider_resource_id:
                return None
            return _Locator(
                owner.provider_resource_id,
                _database_utc(owner.created_at),
            )
        if target.owner_type == RightsTargetOwnerType.CALL_RECORD:
            owner = session.execute(
                tenant_select(CallRecord).where(CallRecord.id == target.owner_id)
            ).scalar_one_or_none()
            if owner is None:
                return None
            locator = {
                RightsTargetResource.CALL: owner.provider_call_id,
                RightsTargetResource.RECORDING: owner.recording_sid,
                RightsTargetResource.TRANSCRIPTION_COLLECTION: owner.recording_sid,
                RightsTargetResource.BLOB_COLLECTION: owner.recording_blob_path,
            }.get(target.resource)
            if not locator:
                return None
            created_at = owner.started_at or owner.created_at
            return _Locator(locator, _database_utc(created_at))
        if target.owner_type == RightsTargetOwnerType.INBOUND_CALL:
            owner = session.execute(
                tenant_select(InboundCall).where(InboundCall.id == target.owner_id)
            ).scalar_one_or_none()
            if owner is None or not owner.provider_call_id:
                return None
            return _Locator(
                owner.provider_call_id,
                _database_utc(owner.created_at),
            )
        if target.owner_type == RightsTargetOwnerType.INBOUND_MESSAGE:
            owner = session.execute(
                tenant_select(InboundMessage).where(InboundMessage.id == target.owner_id)
            ).scalar_one_or_none()
            if owner is None or not owner.provider_message_id:
                return None
            return _Locator(
                owner.provider_message_id,
                _database_utc(owner.created_at),
            )
    return None


def _lock_effect_target(
    session: Session,
    clinic_id: str,
    effect_id: str,
    target_id: str,
) -> tuple[ExternalEffect, RightsTarget]:
    with clinic_scope(session, clinic_id):
        effect_statement = tenant_select(ExternalEffect).where(
            ExternalEffect.id == effect_id
        )
        target_statement = tenant_select(RightsTarget).where(
            RightsTarget.id == target_id,
            RightsTarget.current_effect_id == effect_id,
        )
        if _is_postgresql(session):
            effect_statement = effect_statement.with_for_update()
            target_statement = target_statement.with_for_update()
        effect = session.execute(effect_statement).scalar_one()
        target = session.execute(target_statement).scalar_one()
        return effect, target


def _settle_verified(
    effect: ExternalEffect,
    target: RightsTarget,
    now: datetime,
) -> None:
    target.state = RightsTargetState.VERIFIED
    target.verified_at = target.verified_at or now
    _clear_residual_approval(target)
    _finish_effect(
        effect,
        ExternalEffectState.SUCCEEDED,
        now,
        "verified_absent",
        target.reason_code or "already_absent",
    )


def _settle_residual(
    effect: ExternalEffect,
    target: RightsTarget,
    outcome: RightsAdapterResult,
    approvals: Mapping[RightsResidualCategory, ResidualApproval],
    now: datetime,
) -> bool:
    category = outcome.residual_category
    if category is None:
        _settle_ambiguous(effect, target, now, "residual_category_missing")
        return False
    target.state = RightsTargetState.RESIDUAL
    target.residual_category = category
    target.residual_due_at = outcome.technical_until
    approval = approvals.get(category)
    if (
        approval is not None
        and approval.category == category
        and approval.due_at >= now
        and (
            outcome.technical_until is None
            or approval.due_at >= outcome.technical_until
        )
    ):
        target.residual_policy_version = approval.policy_version
        target.residual_approval_evidence_hash = approval.approval_evidence_hash
        target.residual_completion_eligible = approval.completion_eligible
        target.residual_due_at = approval.due_at
    else:
        target.residual_policy_version = None
        target.residual_approval_evidence_hash = None
        target.residual_completion_eligible = False
    _finish_effect(
        effect,
        ExternalEffectState.SUCCEEDED,
        now,
        "residual",
        outcome.reason.value,
    )
    return True


def _settle_ambiguous(
    effect: ExternalEffect,
    target: RightsTarget,
    now: datetime,
    reason: str,
) -> None:
    effect.state = ExternalEffectState.RECONCILE_REQUIRED
    effect.provider_status = "outcome_unknown"
    effect.last_error_class = "ProviderDispatchError"
    effect.last_error_code = reason
    effect.lease_owner = None
    effect.lease_expires_at = None
    target.state = RightsTargetState.RECONCILE_REQUIRED
    target.available_at = now


def _finish_effect(
    effect: ExternalEffect,
    state: ExternalEffectState,
    now: datetime,
    status: str,
    reason: str,
) -> None:
    effect.state = state
    effect.provider_status = status
    effect.last_error_class = None if state == ExternalEffectState.SUCCEEDED else "RightsEffect"
    effect.last_error_code = reason
    effect.completion_evidence_hash = _evidence_hash(
        effect.request_hash,
        effect.aggregate_id,
        status,
        reason,
    )
    effect.completed_at = now
    effect.lease_owner = None
    effect.lease_expires_at = None


def _clear_residual_approval(target: RightsTarget) -> None:
    target.residual_category = None
    target.residual_policy_version = None
    target.residual_approval_evidence_hash = None
    target.residual_completion_eligible = False
    target.residual_due_at = None


def _recover_target_reconciliation_states(session: Session, clinic_id: str) -> None:
    with clinic_scope(session, clinic_id):
        targets = session.execute(
            tenant_select(RightsTarget)
            .join(
                ExternalEffect,
                sa.and_(
                    ExternalEffect.clinic_id == RightsTarget.clinic_id,
                    ExternalEffect.id == RightsTarget.current_effect_id,
                ),
            )
            .where(
                RightsTarget.state == RightsTargetState.DISPATCHING,
                ExternalEffect.state == ExternalEffectState.RECONCILE_REQUIRED,
            )
        ).scalars()
        for target in targets:
            target.state = RightsTargetState.RECONCILE_REQUIRED


def _recover_stale_reconciliation_claims(
    session: Session,
    clinic_id: str,
    stale_before: datetime,
) -> None:
    with clinic_scope(session, clinic_id):
        targets = session.execute(
            tenant_select(RightsTarget)
            .join(
                ExternalEffect,
                sa.and_(
                    ExternalEffect.clinic_id == RightsTarget.clinic_id,
                    ExternalEffect.id == RightsTarget.current_effect_id,
                ),
            )
            .where(
                RightsTarget.state == RightsTargetState.DISPATCHING,
                RightsTarget.last_reconciled_at.is_not(None),
                RightsTarget.last_reconciled_at <= stale_before,
                ExternalEffect.state == ExternalEffectState.RECONCILE_REQUIRED,
            )
        ).scalars()
        for target in targets:
            target.state = RightsTargetState.RECONCILE_REQUIRED


def _evidence_hash(
    request_hash: str,
    target_id: str,
    status: str,
    reason: str,
) -> str:
    encoded = json.dumps(
        {
            "reason": reason,
            "request_hash": request_hash,
            "status": status,
            "target_id": target_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_worker_args(
    clinic_id: str,
    worker_id: str,
    limit: int,
    max_target_attempts: int,
) -> None:
    if not clinic_id or not worker_id:
        raise ValueError("clinic_id and worker_id are required")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    if not 1 <= max_target_attempts <= 10:
        raise ValueError("max_target_attempts must be between 1 and 10")


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_postgresql(session: Session) -> bool:
    return bool(session.bind is not None and session.bind.dialect.name == "postgresql")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one finite durable rights dispatch or reconciliation batch."""
    parser = argparse.ArgumentParser(
        description="Run one durable Clinic Recall rights batch."
    )
    parser.add_argument("--clinic-id", required=True, help="Internal clinic scope identifier.")
    parser.add_argument(
        "--worker-id",
        default=None,
        help="Lease owner; defaults to the Container Apps Job execution name.",
    )
    parser.add_argument(
        "--mode",
        choices=("dispatch", "reconcile", "both"),
        default="dispatch",
        help="Execute pending effects, reconcile ambiguous outcomes, or do both once.",
    )
    parser.add_argument("--limit", type=int, default=10, help="Maximum targets to inspect.")
    parser.add_argument(
        "--max-target-attempts",
        type=int,
        default=2,
        help="Finite maximum delete attempts per target.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Timezone-aware ISO-8601 timestamp; defaults to current UTC time.",
    )
    args = parser.parse_args(argv)

    now = _parse_now(args.now)
    _bootstrap_runtime_configuration()
    if not durable_rights_enabled():
        result = (
            RightsRunOnceResult(enabled=False)
            if args.mode == "dispatch"
            else RightsReconcileResult(enabled=False)
        )
        print(json.dumps(result.as_summary(), sort_keys=True))
        return 0

    adapters = _runtime_adapters(args.clinic_id)
    if adapters is None:
        result = (
            RightsRunOnceResult(enabled=False, configuration_blocked=1)
            if args.mode == "dispatch"
            else RightsReconcileResult(enabled=False, configuration_blocked=1)
        )
        print(json.dumps(result.as_summary(), sort_keys=True))
        return 2
    completion_configuration = _runtime_completion_configuration()
    if completion_configuration is None:
        result = (
            RightsRunOnceResult(enabled=False, configuration_blocked=1)
            if args.mode in {"dispatch", "both"}
            else RightsReconcileResult(enabled=False, configuration_blocked=1)
        )
        print(json.dumps(result.as_summary(), sort_keys=True))
        return 2
    keyring, residual_approvals = completion_configuration

    common = {
        "clinic_id": args.clinic_id,
        "worker_id": args.worker_id or _default_worker_id(),
        "adapters": adapters,
        "now": now,
        "enabled": True,
        "limit": args.limit,
        "max_target_attempts": args.max_target_attempts,
        "residual_approvals": residual_approvals,
    }
    session_factory = get_privacy_sessionmaker()
    summaries: dict[str, object] = {"enabled": True}
    if args.mode == "both":
        dispatched = run_once(session_factory, **common)
        reconciled = reconcile_once(session_factory, **common)
        summaries["dispatch"] = dispatched.as_summary()
        summaries["reconcile"] = reconciled.as_summary()
    elif args.mode == "dispatch":
        summaries["dispatch"] = run_once(session_factory, **common).as_summary()
    else:
        summaries["reconcile"] = reconcile_once(
            session_factory,
            **common,
        ).as_summary()
    try:
        maintained = _maintain_runtime(
            session_factory,
            clinic_id=args.clinic_id,
            approvals=residual_approvals,
            now=now,
            limit=args.limit,
        )
        summaries["residual_maintenance"] = maintained.as_summary()
        finalized = _finalize_runtime(
            session_factory,
            clinic_id=args.clinic_id,
            keyring=keyring,
            approvals=residual_approvals,
            now=now,
            limit=args.limit,
        )
    except (RuntimeError, ValueError):
        summaries["finalize"] = {"configuration_blocked": 1}
        print(json.dumps(summaries, sort_keys=True))
        return 2
    summaries["finalize"] = finalized.as_summary()
    print(json.dumps(summaries, sort_keys=True))
    return 0


def _runtime_adapters(clinic_id: str) -> dict[RightsTargetSystem, RightsAdapter] | None:
    adapters: dict[RightsTargetSystem, RightsAdapter] = {}
    try:
        if durable_rights_twilio_enabled():
            account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
            auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
            if not account_sid or not auth_token:
                return None
            api_base_url = os.getenv("TWILIO_API_BASE_URL", "").strip()
            adapters[RightsTargetSystem.TWILIO] = TwilioRightsAdapter(
                account_sid=account_sid,
                auth_token=auth_token,
                **({"api_base_url": api_base_url} if api_base_url else {}),
            )
        if durable_rights_blob_enabled():
            account_url = os.getenv("RECORDINGS_BLOB_ACCOUNT_URL", "").strip()
            container_name = os.getenv("RECORDINGS_BLOB_CONTAINER", "").strip()
            if not account_url or not container_name:
                return None
            adapters[RightsTargetSystem.AZURE_BLOB] = AzureBlobRightsAdapter(
                account_url=account_url,
                container_name=container_name,
                clinic_id=clinic_id,
            )
    except (ImportError, RuntimeError, ValueError):
        return None
    return adapters


def _runtime_completion_configuration(
) -> tuple[SubjectKeyring, dict[RightsResidualCategory, ResidualApproval]] | None:
    try:
        return get_rights_subject_keyring(), get_rights_residual_approvals()
    except (RuntimeError, ValueError):
        return None


def _finalize_runtime(
    session_factory,
    *,
    clinic_id: str,
    keyring: SubjectKeyring,
    approvals: dict[RightsResidualCategory, ResidualApproval],
    now: datetime,
    limit: int,
) -> RightsFinalizationResult:
    with session_factory.begin() as session:
        return finalize_ready_patient_erasures(
            session,
            clinic_id=clinic_id,
            keyring=keyring,
            approvals=approvals,
            now=now,
            actor_role="system",
            limit=limit,
        )


def _maintain_runtime(
    session_factory,
    *,
    clinic_id: str,
    approvals: dict[RightsResidualCategory, ResidualApproval],
    now: datetime,
    limit: int,
) -> RightsResidualMaintenanceResult:
    with session_factory.begin() as session:
        return maintain_residual_targets(
            session,
            clinic_id=clinic_id,
            approvals=approvals,
            now=now,
            actor_role="system",
            limit=limit,
        )


def _default_worker_id() -> str:
    value = os.getenv("CONTAINER_APP_JOB_EXECUTION_NAME") or socket.gethostname()
    return value.strip()[:128] or "clinic-recall-rights-worker"


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed.astimezone(UTC)


def _bootstrap_runtime_configuration(
    bootstrap: Callable[[], bool] | None = None,
) -> None:
    if os.getenv("AZURE_APPCONFIG_ENDPOINT", "").strip():
        if bootstrap is None:
            from apps.artagent.backend.config.appconfig_provider import bootstrap_appconfig

            bootstrap = bootstrap_appconfig
        if not bootstrap():
            raise RuntimeError("Azure App Configuration failed to load; rights worker stopped")
    configure_job_telemetry()


if __name__ == "__main__":
    raise SystemExit(main())