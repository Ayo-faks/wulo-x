"""Durable, fail-closed privacy-rights workflow primitives."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .durable.enqueue import enqueue_rights_effect
from .enums import (
    AuditAction,
    BookingWriteBackState,
    CallRecordingStatus,
    ClinicPhoneProvider,
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
    SourceLinkState,
)
from .erasure import erasure_confirm_token
from .messaging.audit import audit_action
from .models import (
    Appointment,
    AvailabilitySlot,
    BookingAction,
    CallRecord,
    Escalation,
    ExternalEffect,
    ExternalEffectHandoff,
    HandoffReceipt,
    InboundCall,
    InboundMessage,
    InboundStaffTask,
    IncidentReport,
    Interaction,
    OutreachJob,
    Patient,
    PatientSourceLink,
    PilotParticipant,
    ProviderCallbackReceipt,
    RightsAliasTombstone,
    RightsRequest,
    RightsTarget,
)

_SID_PATTERNS = {
    RightsTargetResource.MESSAGE: re.compile(r"^(?:SM|MM)[0-9a-fA-F]{32}$"),
    RightsTargetResource.CALL: re.compile(r"^CA[0-9a-fA-F]{32}$"),
    RightsTargetResource.RECORDING: re.compile(r"^RE[0-9a-fA-F]{32}$"),
}
_CANCELED_REASON = "subject_frozen"
_UNSTABLE_RECORDING_STATES = frozenset(
    {
        CallRecordingStatus.PENDING,
        CallRecordingStatus.START_PENDING,
        CallRecordingStatus.STARTING,
        CallRecordingStatus.IN_PROGRESS,
        CallRecordingStatus.STOP_PENDING,
        CallRecordingStatus.STOPPING,
        CallRecordingStatus.RECONCILE_REQUIRED,
    }
)


class SubjectFrozenError(RuntimeError):
    """Raised when work would recreate or contact an erased subject."""

    def __init__(self) -> None:
        super().__init__("subject_frozen")


class RightsCompletionBlocked(RuntimeError):
    """A rights request has not met a deterministic completion precondition."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SubjectKey:
    """One version of secret material used for non-reidentifying tombstones."""

    version: str
    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.version or len(self.version) > 64:
            raise ValueError("subject key version must contain 1 to 64 characters")
        if len(self.secret) < 16:
            raise ValueError("subject key secret must contain at least 16 bytes")


@dataclass(frozen=True)
class SubjectKeyring:
    """Current and retained key versions used to recognize old tombstones."""

    current: SubjectKey
    previous: tuple[SubjectKey, ...] = ()

    def __post_init__(self) -> None:
        versions = [key.version for key in self.keys]
        if len(versions) != len(set(versions)):
            raise ValueError("subject key versions must be unique")

    @property
    def keys(self) -> tuple[SubjectKey, ...]:
        return (self.current, *self.previous)


@dataclass(frozen=True)
class RightsPolicy:
    """Trusted, versioned authority supplied by configuration, not request input."""

    version: str
    approval_evidence_hash: str
    request_due_after: timedelta

    def __post_init__(self) -> None:
        if not self.version or len(self.version) > 128:
            raise ValueError("rights policy version must contain 1 to 128 characters")
        _require_sha256(self.approval_evidence_hash, "approval_evidence_hash")
        if self.request_due_after <= timedelta(0):
            raise ValueError("rights policy request_due_after must be positive")


@dataclass(frozen=True)
class RightsRequestResult:
    """Minimized response from a durable rights-request transaction."""

    request_id: str
    state: RightsRequestState
    created: bool
    target_count: int
    due_at: datetime


@dataclass(frozen=True)
class ResidualApproval:
    """Trusted policy approval for one residual category and absolute due time."""

    category: RightsResidualCategory
    policy_version: str
    approval_evidence_hash: str
    due_at: datetime
    completion_eligible: bool

    def __post_init__(self) -> None:
        if not self.policy_version or len(self.policy_version) > 128:
            raise ValueError("residual policy_version is required")
        _require_sha256(self.approval_evidence_hash, "approval_evidence_hash")
        _aware_utc(self.due_at, "residual due_at")


@dataclass(frozen=True)
class RightsRequestStatus:
    """Aggregate-only rights state safe for staff APIs and telemetry."""

    request_id: str
    state: RightsRequestState
    target_count: int
    pending_count: int
    verified_count: int
    residual_count: int
    unapproved_residual_count: int
    overdue_count: int
    requested_at: datetime
    due_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class RightsOperationsStatus:
    """Clinic-scoped rights readiness counters without subject identifiers."""

    request_count: int
    incomplete_request_count: int
    target_count: int
    pending_count: int
    reconcile_required_count: int
    handoff_count: int
    unapproved_residual_count: int
    overdue_count: int
    zero_overdue: bool
    ready: bool


@dataclass(frozen=True)
class RightsFinalizationResult:
    """Aggregate-only outcome from one bounded erasure finalization pass."""

    inspected_count: int
    completed_count: int
    blocked_count: int
    approvals_applied: int

    def as_summary(self) -> dict[str, int]:
        return {
            "inspected_count": self.inspected_count,
            "completed_count": self.completed_count,
            "blocked_count": self.blocked_count,
            "approvals_applied": self.approvals_applied,
        }


@dataclass(frozen=True)
class RightsResidualMaintenanceResult:
    """Aggregate-only outcome from one bounded residual maintenance pass."""

    inspected_count: int
    approvals_applied: int
    overdue_count: int

    def as_summary(self) -> dict[str, int]:
        return {
            "inspected_count": self.inspected_count,
            "approvals_applied": self.approvals_applied,
            "overdue_count": self.overdue_count,
        }


@dataclass(frozen=True)
class _TargetSpec:
    system: RightsTargetSystem
    resource: RightsTargetResource
    action: RightsTargetAction
    owner_type: RightsTargetOwnerType
    owner_id: str
    locator: str = field(repr=False)
    dispatchable: bool = True
    dispatch_ready: bool = True
    initial_state: RightsTargetState = RightsTargetState.REQUESTED
    residual_category: RightsResidualCategory | None = None


def request_patient_erasure(
    session: Session,
    *,
    clinic_id: str,
    patient_id: str,
    confirm_token: str,
    request_identity: str,
    actor_role: str,
    actor_reference: str,
    keyring: SubjectKeyring,
    policy: RightsPolicy,
    now: datetime,
) -> RightsRequestResult:
    """Freeze and inventory one patient in the caller's transaction.

    This function deliberately accepts no provider adapter and performs no
    network I/O. The caller must commit before a finite RIGHTS worker can claim
    any generated effect.
    """
    now = _aware_utc(now, "now")
    clinic_id = _required(clinic_id, "clinic_id")
    patient_id = _required(patient_id, "patient_id")
    request_identity = _required(request_identity, "request_identity")
    actor_role = _required(actor_role, "actor_role")
    actor_reference = _required(actor_reference, "actor_reference")
    if confirm_token != erasure_confirm_token(patient_id):
        raise ValueError(f"confirm_token must be {erasure_confirm_token(patient_id)!r}")

    with clinic_scope(session, clinic_id):
        patient_reference_hashes = {
            _keyed_hash(key, "patient-reference", patient_id) for key in keyring.keys
        }
        existing_by_reference = session.execute(
            tenant_select(RightsRequest).where(
                RightsRequest.kind == RightsRequestKind.ERASURE,
                RightsRequest.patient_reference_hash.in_(patient_reference_hashes),
            )
        ).scalar_one_or_none()
        if existing_by_reference is not None:
            audit_action(
                session,
                clinic_id,
                AuditAction.ERASE_PATIENT,
                existing_by_reference.id,
                {
                    "request_id": existing_by_reference.id,
                    "created": False,
                    "state": existing_by_reference.state.value,
                    "occurred_at": now,
                },
                actor=f"role:{actor_role}",
            )
            session.flush()
            return _result(existing_by_reference, created=False)

        patient = session.execute(
            tenant_select(Patient).where(Patient.id == patient_id)
        ).scalar_one_or_none()
        if patient is None:
            raise LookupError(f"patient {patient_id!r} not found for clinic")
        source_ref = patient.source_ref
        canonical_subject = _canonical_subject(clinic_id, source_ref)
        _lock_subject_identity(
            session,
            _keyed_hash(keyring.current, "subject", canonical_subject),
        )

        patient_statement = tenant_select(Patient).where(Patient.id == patient_id)
        if _is_postgresql(session):
            patient_statement = patient_statement.with_for_update()
        patient = session.execute(patient_statement).scalar_one_or_none()
        if patient is None:
            raise LookupError(f"patient {patient_id!r} not found for clinic")
        if patient.source_ref != source_ref:
            raise RuntimeError("patient source identity changed while requesting erasure")

        subject_hashes = {
            _keyed_hash(key, "subject", canonical_subject) for key in keyring.keys
        }
        existing_statement = tenant_select(RightsRequest).where(
            RightsRequest.kind == RightsRequestKind.ERASURE,
            RightsRequest.subject_key_hash.in_(subject_hashes),
        )
        if _is_postgresql(session):
            existing_statement = existing_statement.with_for_update()
        existing = session.execute(existing_statement).scalar_one_or_none()
        if existing is not None:
            audit_action(
                session,
                clinic_id,
                AuditAction.ERASE_PATIENT,
                existing.id,
                {
                    "request_id": existing.id,
                    "created": False,
                    "state": existing.state.value,
                    "occurred_at": now,
                },
                actor=f"role:{actor_role}",
            )
            session.flush()
            return _result(existing, created=False)

        subject_key_hash = _keyed_hash(keyring.current, "subject", canonical_subject)
        request = RightsRequest(
            id=f"rights-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            kind=RightsRequestKind.ERASURE,
            subject_key_hash=subject_key_hash,
            subject_key_version=keyring.current.version,
            patient_reference_hash=_keyed_hash(
                keyring.current,
                "patient-reference",
                patient.id,
            ),
            patient_id=patient.id,
            request_identity_hash=_keyed_hash(
                keyring.current,
                "request-identity",
                request_identity,
            ),
            actor_role=actor_role,
            actor_reference_hash=_keyed_hash(
                keyring.current,
                "actor-reference",
                actor_reference,
            ),
            policy_version=policy.version,
            approval_evidence_hash=policy.approval_evidence_hash,
            scope_hash=_keyed_hash(keyring.current, "erasure-scope", canonical_subject),
            state=RightsRequestState.REQUESTED,
            requested_at=now,
            due_at=now + policy.request_due_after,
        )
        session.add(request)
        session.flush()

        _freeze_source_aliases(session, clinic_id, patient.id, request, keyring)

        job_ids = _patient_job_ids(session, patient.id)
        effects = _patient_effects(session, job_ids)
        call_records = _patient_call_records(
            session,
            patient.id,
            {effect.id for effect in effects},
        )
        call_record_ids = {record.id for record in call_records}
        effects = _merge_effects(
            effects,
            _call_record_effects(session, call_record_ids),
        )
        inbound_calls, inbound_messages = _patient_inbound_owners(
            session,
            patient.id,
        )
        incidents = _patient_incidents(session, job_ids)

        _cancel_undispatched(session, effects, call_records, now)
        _request_active_recording_stops(session, call_records, now)
        effects = _merge_effects(
            effects,
            _call_record_effects(session, call_record_ids),
        )
        specs = _target_specs(
            keyring.current,
            effects,
            call_records,
            inbound_calls,
            inbound_messages,
            incidents,
            request.id,
        )
        for spec, target_key_hash in specs:
            target = RightsTarget(
                id=f"rights-target-{uuid.uuid4().hex}",
                clinic_id=clinic_id,
                request_id=request.id,
                system=spec.system,
                resource=spec.resource,
                action=spec.action,
                owner_type=spec.owner_type,
                owner_id=spec.owner_id,
                target_key_hash=target_key_hash,
                mandatory=True,
                state=spec.initial_state,
                attempt_ordinal=(
                    1 if spec.dispatchable and spec.dispatch_ready else 0
                ),
                available_at=now,
                due_at=request.due_at,
                residual_category=spec.residual_category,
            )
            session.add(target)
            session.flush()
            if spec.dispatchable and spec.dispatch_ready:
                effect, _ = enqueue_rights_effect(
                    session,
                    clinic_id=clinic_id,
                    target_id=target.id,
                    attempt_ordinal=target.attempt_ordinal,
                    available_at=now,
                )
                target.current_effect_id = effect.id

        request.target_count = len(specs)
        request.state = RightsRequestState.FROZEN
        request.frozen_at = now
        if _inventory_is_final(effects, call_records):
            request.inventory_finalized_at = now
        audit_action(
            session,
            clinic_id,
            AuditAction.ERASE_PATIENT,
            request.id,
            {
                "request_id": request.id,
                "created": True,
                "state": request.state.value,
                "target_count": request.target_count,
                "occurred_at": now,
            },
            actor=f"role:{actor_role}",
        )
        session.flush()
        return _result(request, created=True)


def subject_key_hashes(
    clinic_id: str,
    source_ref: str,
    keyring: SubjectKeyring,
) -> frozenset[str]:
    """Return current and retained tombstone digests for a canonical subject."""
    canonical = _canonical_subject(clinic_id, source_ref)
    return frozenset(_keyed_hash(key, "subject", canonical) for key in keyring.keys)


def apply_residual_approvals(
    session: Session,
    *,
    clinic_id: str,
    request_id: str,
    approvals: dict[RightsResidualCategory, ResidualApproval],
    now: datetime,
    actor_role: str,
) -> int:
    """Bind trusted policy evidence to matching residual targets."""
    now = _aware_utc(now, "now")
    actor_role = _required(actor_role, "actor_role")
    with clinic_scope(session, clinic_id):
        request_statement = tenant_select(RightsRequest).where(
            RightsRequest.id == request_id
        )
        if _is_postgresql(session):
            request_statement = request_statement.with_for_update()
        request = session.execute(request_statement).scalar_one_or_none()
        if request is None:
            raise LookupError("rights request not found for clinic")
        targets_statement = tenant_select(RightsTarget).where(
            RightsTarget.request_id == request.id,
            RightsTarget.state == RightsTargetState.RESIDUAL,
        )
        if _is_postgresql(session):
            targets_statement = targets_statement.with_for_update()
        targets = list(session.execute(targets_statement).scalars())
        updated = 0
        for target in targets:
            category = target.residual_category
            approval = approvals.get(category) if category is not None else None
            if approval is None:
                continue
            if approval.category != category:
                raise ValueError("residual approval category does not match mapping key")
            due_at = _aware_utc(approval.due_at, "residual due_at")
            if due_at < now:
                raise ValueError("residual approval is already overdue")
            technical_until = target.residual_due_at
            if technical_until is not None and due_at < _database_utc(technical_until):
                raise ValueError("residual approval ends before the documented technical window")
            if (
                target.residual_policy_version == approval.policy_version
                and target.residual_approval_evidence_hash
                == approval.approval_evidence_hash
                and target.residual_completion_eligible
                == approval.completion_eligible
                and target.residual_due_at is not None
                and _database_utc(target.residual_due_at) == due_at
            ):
                continue
            target.residual_policy_version = approval.policy_version
            target.residual_approval_evidence_hash = approval.approval_evidence_hash
            target.residual_completion_eligible = approval.completion_eligible
            target.residual_due_at = due_at
            updated += 1
        if updated:
            audit_action(
                session,
                clinic_id,
                AuditAction.ERASE_PATIENT,
                request.id,
                {
                    "request_id": request.id,
                    "residual_approvals_applied": updated,
                    "occurred_at": now,
                },
                actor=f"role:{actor_role}",
            )
        session.flush()
        return updated


def finalize_ready_patient_erasures(
    session: Session,
    *,
    clinic_id: str,
    keyring: SubjectKeyring,
    approvals: dict[RightsResidualCategory, ResidualApproval],
    now: datetime,
    actor_role: str,
    limit: int = 10,
) -> RightsFinalizationResult:
    """Apply trusted residual policy and complete each currently ready erasure."""
    now = _aware_utc(now, "now")
    actor_role = _required(actor_role, "actor_role")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    with clinic_scope(session, clinic_id):
        statement = (
            tenant_select(RightsRequest)
            .where(
                RightsRequest.kind == RightsRequestKind.ERASURE,
                RightsRequest.state != RightsRequestState.COMPLETED,
            )
            .order_by(RightsRequest.requested_at, RightsRequest.id)
            .limit(limit)
        )
        if _is_postgresql(session):
            statement = statement.with_for_update(skip_locked=True)
        requests = list(session.execute(statement).scalars())
        request_ids = [request.id for request in requests]

    completed = 0
    blocked = 0
    approvals_applied = 0
    for request_id in request_ids:
        approvals_applied += apply_residual_approvals(
            session,
            clinic_id=clinic_id,
            request_id=request_id,
            approvals=approvals,
            now=now,
            actor_role=actor_role,
        )
        try:
            status = complete_patient_erasure(
                session,
                clinic_id=clinic_id,
                request_id=request_id,
                keyring=keyring,
                now=now,
                actor_role=actor_role,
            )
        except RightsCompletionBlocked:
            blocked += 1
            continue
        if status.state == RightsRequestState.COMPLETED:
            completed += 1
    return RightsFinalizationResult(
        inspected_count=len(request_ids),
        completed_count=completed,
        blocked_count=blocked,
        approvals_applied=approvals_applied,
    )


def maintain_residual_targets(
    session: Session,
    *,
    clinic_id: str,
    approvals: dict[RightsResidualCategory, ResidualApproval],
    now: datetime,
    actor_role: str,
    limit: int = 50,
) -> RightsResidualMaintenanceResult:
    """Renew explicit residual approvals and report any overdue evidence."""
    now = _aware_utc(now, "now")
    actor_role = _required(actor_role, "actor_role")
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    with clinic_scope(session, clinic_id):
        statement = (
            tenant_select(RightsTarget)
            .where(RightsTarget.state == RightsTargetState.RESIDUAL)
            .order_by(RightsTarget.residual_due_at, RightsTarget.id)
            .limit(limit)
        )
        if _is_postgresql(session):
            statement = statement.with_for_update(skip_locked=True)
        targets = list(session.execute(statement).scalars())
        changed_request_ids: set[str] = set()
        approvals_applied = 0
        overdue = 0
        for target in targets:
            category = target.residual_category
            current_due = (
                _database_utc(target.residual_due_at)
                if target.residual_due_at is not None
                else None
            )
            approval = approvals.get(category) if category is not None else None
            if approval is not None:
                if approval.category != category:
                    raise ValueError("residual approval category does not match mapping key")
                approval_due = _aware_utc(approval.due_at, "residual due_at")
                if approval_due < now:
                    raise ValueError("residual approval is already overdue")
                if current_due is not None and approval_due < current_due:
                    raise ValueError(
                        "residual approval ends before the current approved window"
                    )
                if not (
                    target.residual_policy_version == approval.policy_version
                    and target.residual_approval_evidence_hash
                    == approval.approval_evidence_hash
                    and target.residual_completion_eligible
                    == approval.completion_eligible
                    and current_due == approval_due
                ):
                    target.residual_policy_version = approval.policy_version
                    target.residual_approval_evidence_hash = (
                        approval.approval_evidence_hash
                    )
                    target.residual_completion_eligible = (
                        approval.completion_eligible
                    )
                    target.residual_due_at = approval_due
                    changed_request_ids.add(target.request_id)
                    approvals_applied += 1
                    current_due = approval_due
            if current_due is not None and current_due < now:
                overdue += 1

        for request_id in sorted(changed_request_ids):
            request_statement = tenant_select(RightsRequest).where(
                RightsRequest.id == request_id
            )
            target_statement = tenant_select(RightsTarget).where(
                RightsTarget.request_id == request_id
            )
            if _is_postgresql(session):
                request_statement = request_statement.with_for_update()
                target_statement = target_statement.with_for_update()
            request = session.execute(request_statement).scalar_one()
            request_targets = list(session.execute(target_statement).scalars())
            request.target_count = len(request_targets)
            request.verified_target_count = sum(
                item.state == RightsTargetState.VERIFIED
                for item in request_targets
            )
            request.residual_target_count = sum(
                item.state == RightsTargetState.RESIDUAL
                for item in request_targets
            )
            if request.state == RightsRequestState.COMPLETED:
                request.completion_evidence_hash = _completion_evidence_hash(
                    request,
                    request_targets,
                )
            audit_action(
                session,
                clinic_id,
                AuditAction.ERASE_PATIENT,
                request.id,
                {
                    "request_id": request.id,
                    "residual_approvals_applied": approvals_applied,
                    "occurred_at": now,
                },
                actor=f"role:{actor_role}",
            )
        session.flush()
    return RightsResidualMaintenanceResult(
        inspected_count=len(targets),
        approvals_applied=approvals_applied,
        overdue_count=overdue,
    )


def get_rights_request_status(
    session: Session,
    *,
    clinic_id: str,
    request_id: str,
    now: datetime,
) -> RightsRequestStatus:
    """Return only aggregate workflow state and bounded residual counts."""
    now = _aware_utc(now, "now")
    with clinic_scope(session, clinic_id):
        request = session.execute(
            tenant_select(RightsRequest).where(RightsRequest.id == request_id)
        ).scalar_one_or_none()
        if request is None:
            raise LookupError("rights request not found for clinic")
        targets = list(
            session.execute(
                tenant_select(RightsTarget).where(RightsTarget.request_id == request.id)
            ).scalars()
        )
    verified = sum(target.state == RightsTargetState.VERIFIED for target in targets)
    residuals = [target for target in targets if target.state == RightsTargetState.RESIDUAL]
    unapproved = sum(not _residual_is_approved(target, now) for target in residuals)
    overdue = sum(
        (
            _database_utc(target.due_at) < now
            and target.state not in {
                RightsTargetState.VERIFIED,
                RightsTargetState.RESIDUAL,
            }
        )
        or (
            target.state == RightsTargetState.RESIDUAL
            and target.residual_due_at is not None
            and _database_utc(target.residual_due_at) < now
        )
        for target in targets
    )
    return RightsRequestStatus(
        request_id=request.id,
        state=request.state,
        target_count=len(targets),
        pending_count=len(targets) - verified - len(residuals),
        verified_count=verified,
        residual_count=len(residuals),
        unapproved_residual_count=unapproved,
        overdue_count=overdue,
        requested_at=_database_utc(request.requested_at),
        due_at=_database_utc(request.due_at),
        completed_at=(
            _database_utc(request.completed_at)
            if request.completed_at is not None
            else None
        ),
    )


def get_rights_operations_status(
    session: Session,
    *,
    clinic_id: str,
    now: datetime,
) -> RightsOperationsStatus:
    """Return aggregate counters for the clinic's rights operational gate."""
    now = _aware_utc(now, "now")
    with clinic_scope(session, clinic_id):
        requests = list(session.execute(tenant_select(RightsRequest)).scalars())
        targets = list(session.execute(tenant_select(RightsTarget)).scalars())
        handoff_count = int(
            session.scalar(
                sa.select(sa.func.count())
                .select_from(ExternalEffectHandoff)
                .join(
                    ExternalEffect,
                    sa.and_(
                        ExternalEffect.clinic_id
                        == ExternalEffectHandoff.clinic_id,
                        ExternalEffect.id
                        == ExternalEffectHandoff.external_effect_id,
                    ),
                )
                .where(
                    ExternalEffectHandoff.clinic_id == clinic_id,
                    ExternalEffect.effect_type == ExternalEffectType.RIGHTS,
                )
            )
            or 0
        )
    pending = [
        target
        for target in targets
        if target.state not in {
            RightsTargetState.VERIFIED,
            RightsTargetState.RESIDUAL,
        }
    ]
    residuals = [
        target for target in targets if target.state == RightsTargetState.RESIDUAL
    ]
    overdue_count = sum(
        (
            target in pending
            and _database_utc(target.due_at) < now
        )
        or (
            target.state == RightsTargetState.RESIDUAL
            and target.residual_due_at is not None
            and _database_utc(target.residual_due_at) < now
        )
        for target in targets
    )
    incomplete_request_count = sum(
        request.state != RightsRequestState.COMPLETED for request in requests
    )
    reconcile_required_count = sum(
        target.state == RightsTargetState.RECONCILE_REQUIRED for target in targets
    )
    unapproved_residual_count = sum(
        not _residual_is_approved(target, now) for target in residuals
    )
    zero_overdue = overdue_count == 0
    ready = (
        incomplete_request_count == 0
        and not pending
        and reconcile_required_count == 0
        and handoff_count == 0
        and unapproved_residual_count == 0
        and zero_overdue
    )
    return RightsOperationsStatus(
        request_count=len(requests),
        incomplete_request_count=incomplete_request_count,
        target_count=len(targets),
        pending_count=len(pending),
        reconcile_required_count=reconcile_required_count,
        handoff_count=handoff_count,
        unapproved_residual_count=unapproved_residual_count,
        overdue_count=overdue_count,
        zero_overdue=zero_overdue,
        ready=ready,
    )


def complete_patient_erasure(
    session: Session,
    *,
    clinic_id: str,
    request_id: str,
    keyring: SubjectKeyring,
    now: datetime,
    actor_role: str,
) -> RightsRequestStatus:
    """Atomically minimize a frozen patient after every target is settled."""
    now = _aware_utc(now, "now")
    actor_role = _required(actor_role, "actor_role")
    with clinic_scope(session, clinic_id):
        request = _load_request_for_update(session, request_id)
        if request.kind != RightsRequestKind.ERASURE:
            raise RightsCompletionBlocked("request_kind_not_erasure")
        if request.state == RightsRequestState.COMPLETED:
            return get_rights_request_status(
                session,
                clinic_id=clinic_id,
                request_id=request.id,
                now=now,
            )
        _refresh_patient_erasure_inventory(
            session,
            request=request,
            keyring=keyring,
            now=now,
        )
        targets = _load_targets_for_update(session, request.id)
        local_target = next(
            (
                target
                for target in targets
                if target.system == RightsTargetSystem.LOCAL
                and target.resource == RightsTargetResource.PATIENT_GRAPH
            ),
            None,
        )
        if local_target is None:
            raise RightsCompletionBlocked("local_target_missing")
        if request.inventory_finalized_at is None:
            raise RightsCompletionBlocked("inventory_not_finalized")
        for target in targets:
            if target.id == local_target.id:
                continue
            if target.state == RightsTargetState.VERIFIED:
                continue
            if target.state == RightsTargetState.RESIDUAL:
                if not _residual_is_approved(target, now):
                    raise RightsCompletionBlocked("unapproved_residual")
                continue
            if _database_utc(target.due_at) < now:
                raise RightsCompletionBlocked("overdue_target")
            raise RightsCompletionBlocked("pending_target")

        request.state = RightsRequestState.DELETING
        _minimize_patient_graph(
            session,
            request=request,
            targets=targets,
            now=now,
        )
        local_target.state = RightsTargetState.VERIFIED
        local_target.verified_at = now
        local_target.locator_cleared_at = now
        local_target.disposition_code = "local_graph_minimized"
        local_target.reason_code = "local_graph_minimized"
        session.flush()

        request.verified_target_count = sum(
            target.state == RightsTargetState.VERIFIED for target in targets
        )
        request.residual_target_count = sum(
            target.state == RightsTargetState.RESIDUAL for target in targets
        )
        request.target_count = len(targets)
        request.state = RightsRequestState.COMPLETED
        request.completed_at = now
        request.completion_evidence_hash = _completion_evidence_hash(request, targets)
        audit_action(
            session,
            clinic_id,
            AuditAction.ERASE_PATIENT,
            request.id,
            {
                "request_id": request.id,
                "state": request.state.value,
                "target_count": request.target_count,
                "verified_target_count": request.verified_target_count,
                "residual_target_count": request.residual_target_count,
                "occurred_at": now,
            },
            actor=f"role:{actor_role}",
        )
        session.flush()
        return get_rights_request_status(
            session,
            clinic_id=clinic_id,
            request_id=request.id,
            now=now,
        )


def _load_request_for_update(session: Session, request_id: str) -> RightsRequest:
    statement = tenant_select(RightsRequest).where(RightsRequest.id == request_id)
    if _is_postgresql(session):
        statement = statement.with_for_update()
    request = session.execute(statement).scalar_one_or_none()
    if request is None:
        raise LookupError("rights request not found for clinic")
    return request


def _load_targets_for_update(
    session: Session,
    request_id: str,
) -> list[RightsTarget]:
    statement = tenant_select(RightsTarget).where(RightsTarget.request_id == request_id)
    if _is_postgresql(session):
        statement = statement.with_for_update()
    return list(session.execute(statement).scalars())


def _refresh_patient_erasure_inventory(
    session: Session,
    *,
    request: RightsRequest,
    keyring: SubjectKeyring,
    now: datetime,
) -> None:
    if request.patient_id is None:
        raise RightsCompletionBlocked("patient_reference_missing")
    patient_statement = tenant_select(Patient).where(Patient.id == request.patient_id)
    if _is_postgresql(session):
        patient_statement = patient_statement.with_for_update()
    patient = session.execute(patient_statement).scalar_one_or_none()
    if patient is None:
        raise RightsCompletionBlocked("patient_missing")
    key = next(
        (candidate for candidate in keyring.keys if candidate.version == request.subject_key_version),
        None,
    )
    if key is None:
        raise RightsCompletionBlocked("subject_key_version_unavailable")

    job_ids = _patient_job_ids(session, patient.id)
    effects = _patient_effects(session, job_ids)
    call_records = _patient_call_records(
        session,
        patient.id,
        {effect.id for effect in effects},
    )
    call_record_ids = {record.id for record in call_records}
    effects = _merge_effects(effects, _call_record_effects(session, call_record_ids))
    _cancel_undispatched(session, effects, call_records, now)
    _request_active_recording_stops(session, call_records, now)
    effects = _merge_effects(effects, _call_record_effects(session, call_record_ids))
    inbound_calls, inbound_messages = _patient_inbound_owners(session, patient.id)
    incidents = _patient_incidents(session, job_ids)
    specs = _target_specs(
        key,
        effects,
        call_records,
        inbound_calls,
        inbound_messages,
        incidents,
        request.id,
    )
    targets = _load_targets_for_update(session, request.id)
    by_hash = {target.target_key_hash: target for target in targets}
    for spec, target_key_hash in specs:
        target = by_hash.get(target_key_hash)
        if target is None:
            target = RightsTarget(
                id=f"rights-target-{uuid.uuid4().hex}",
                clinic_id=request.clinic_id,
                request_id=request.id,
                system=spec.system,
                resource=spec.resource,
                action=spec.action,
                owner_type=spec.owner_type,
                owner_id=spec.owner_id,
                target_key_hash=target_key_hash,
                mandatory=True,
                state=spec.initial_state,
                attempt_ordinal=(
                    1 if spec.dispatchable and spec.dispatch_ready else 0
                ),
                available_at=now,
                due_at=request.due_at,
                residual_category=spec.residual_category,
            )
            session.add(target)
            session.flush()
            by_hash[target_key_hash] = target
        if (
            spec.dispatchable
            and spec.dispatch_ready
            and target.state == RightsTargetState.REQUESTED
            and target.current_effect_id is None
        ):
            target.attempt_ordinal = max(target.attempt_ordinal, 1)
            effect, _ = enqueue_rights_effect(
                session,
                clinic_id=request.clinic_id,
                target_id=target.id,
                attempt_ordinal=target.attempt_ordinal,
                available_at=max(_database_utc(target.available_at), now),
            )
            target.current_effect_id = effect.id
    request.target_count = len(by_hash)
    request.inventory_finalized_at = (
        now if _inventory_is_final(effects, call_records) else None
    )
    session.flush()


def _request_active_recording_stops(
    session: Session,
    call_records: list[CallRecord],
    now: datetime,
) -> None:
    from .enums import CallRecordingStatus

    for record in call_records:
        if (
            record.recording_status == CallRecordingStatus.IN_PROGRESS
            and record.recording_sid
        ):
            from .recording import request_recording_stop

            request_recording_stop(
                session,
                clinic_id=record.clinic_id,
                call_record_id=record.id,
                now=now,
            )


def _minimize_patient_graph(
    session: Session,
    *,
    request: RightsRequest,
    targets: list[RightsTarget],
    now: datetime,
) -> None:
    patient_id = request.patient_id
    if patient_id is None:
        raise RightsCompletionBlocked("patient_reference_missing")
    clinic_id = request.clinic_id
    job_ids = tuple(
        session.execute(
            sa.select(OutreachJob.id).where(
                OutreachJob.clinic_id == clinic_id,
                OutreachJob.patient_id == patient_id,
            )
        ).scalars()
    )
    appointment_ids = tuple(
        session.execute(
            sa.select(Appointment.id).where(
                Appointment.clinic_id == clinic_id,
                Appointment.patient_id == patient_id,
            )
        ).scalars()
    )
    effect_ids = {
        target.owner_id
        for target in targets
        if target.owner_type == RightsTargetOwnerType.EXTERNAL_EFFECT
    }
    effect_ids.update(
        session.execute(
            sa.select(ExternalEffect.id).where(
                ExternalEffect.clinic_id == clinic_id,
                ExternalEffect.aggregate_type == "outreach_job",
                ExternalEffect.aggregate_id.in_(job_ids) if job_ids else sa.false(),
                ExternalEffect.effect_type != ExternalEffectType.RIGHTS,
            )
        ).scalars()
    )
    call_record_ids = {
        target.owner_id
        for target in targets
        if target.owner_type == RightsTargetOwnerType.CALL_RECORD
    }
    call_record_ids.update(
        session.execute(
            sa.select(CallRecord.id).where(
                CallRecord.clinic_id == clinic_id,
                CallRecord.patient_id == patient_id,
            )
        ).scalars()
    )
    inbound_call_ids = {
        target.owner_id
        for target in targets
        if target.owner_type == RightsTargetOwnerType.INBOUND_CALL
    }
    inbound_message_ids = {
        target.owner_id
        for target in targets
        if target.owner_type == RightsTargetOwnerType.INBOUND_MESSAGE
    }

    _remove_patient_linked_handoff_evidence(
        session,
        clinic_id=clinic_id,
        patient_id=patient_id,
        job_ids=job_ids,
        appointment_ids=appointment_ids,
        inbound_call_ids=inbound_call_ids,
        inbound_message_ids=inbound_message_ids,
    )

    _require_owner_targets_settled(targets, now)
    session.execute(
        sa.update(PilotParticipant)
        .where(
            PilotParticipant.clinic_id == clinic_id,
            PilotParticipant.patient_id == patient_id,
        )
        .values(patient_id=None)
    )
    session.execute(
        sa.update(ProviderCallbackReceipt)
        .where(
            ProviderCallbackReceipt.clinic_id == clinic_id,
            ProviderCallbackReceipt.external_effect_id.in_(effect_ids)
            if effect_ids
            else sa.false(),
        )
        .values(provider_resource_id=None)
    )
    for effect in session.execute(
        tenant_select(ExternalEffect).where(
            ExternalEffect.id.in_(effect_ids) if effect_ids else sa.false()
        )
    ).scalars():
        effect.aggregate_type = "rights_minimized"
        effect.aggregate_id = request.id
        effect.idempotency_key = f"minimized:{effect.id}"
        effect.payload = {"intent": "minimized"}
        effect.provider_resource_id = None
        effect.provider_status = "minimized"

    if call_record_ids:
        session.execute(
            sa.delete(CallRecord).where(
                CallRecord.clinic_id == clinic_id,
                CallRecord.id.in_(call_record_ids),
            )
        )
    task_filter = sa.or_(
        InboundStaffTask.patient_id == patient_id,
        InboundStaffTask.inbound_call_id.in_(inbound_call_ids)
        if inbound_call_ids
        else sa.false(),
        InboundStaffTask.inbound_message_id.in_(inbound_message_ids)
        if inbound_message_ids
        else sa.false(),
    )
    session.execute(
        sa.delete(InboundStaffTask).where(
            InboundStaffTask.clinic_id == clinic_id,
            task_filter,
        )
    )
    if inbound_call_ids:
        session.execute(
            sa.delete(InboundCall).where(
                InboundCall.clinic_id == clinic_id,
                InboundCall.id.in_(inbound_call_ids),
            )
        )
    if inbound_message_ids:
        session.execute(
            sa.delete(InboundMessage).where(
                InboundMessage.clinic_id == clinic_id,
                InboundMessage.id.in_(inbound_message_ids),
            )
        )
    if appointment_ids:
        session.execute(
            sa.update(AvailabilitySlot)
            .where(
                AvailabilitySlot.clinic_id == clinic_id,
                AvailabilitySlot.appointment_id.in_(appointment_ids),
            )
            .values(appointment_id=None, details=None)
        )
    if job_ids:
        session.execute(
            sa.update(IncidentReport)
            .where(
                IncidentReport.clinic_id == clinic_id,
                IncidentReport.related_job_id.in_(job_ids),
            )
            .values(related_job_id=None)
        )
        session.execute(
            sa.delete(Interaction).where(
                Interaction.clinic_id == clinic_id,
                Interaction.outreach_job_id.in_(job_ids),
            )
        )
    booking_conditions = []
    if appointment_ids:
        booking_conditions.append(BookingAction.appointment_id.in_(appointment_ids))
    if job_ids:
        booking_conditions.append(BookingAction.outreach_job_id.in_(job_ids))
    if booking_conditions:
        session.execute(
            sa.delete(BookingAction).where(
                BookingAction.clinic_id == clinic_id,
                sa.or_(*booking_conditions),
            )
        )
    session.execute(
        sa.delete(Escalation).where(
            Escalation.clinic_id == clinic_id,
            Escalation.patient_id == patient_id,
        )
    )
    if job_ids:
        session.execute(
            sa.delete(OutreachJob).where(
                OutreachJob.clinic_id == clinic_id,
                OutreachJob.id.in_(job_ids),
            )
        )
    if appointment_ids:
        session.execute(
            sa.delete(Appointment).where(
                Appointment.clinic_id == clinic_id,
                Appointment.id.in_(appointment_ids),
            )
        )
    request.patient_id = None
    session.flush()
    session.execute(
        sa.delete(Patient).where(
            Patient.clinic_id == clinic_id,
            Patient.id == patient_id,
        )
    )
    for target in targets:
        if target.owner_type in {
            RightsTargetOwnerType.EXTERNAL_EFFECT,
            RightsTargetOwnerType.CALL_RECORD,
            RightsTargetOwnerType.INBOUND_CALL,
            RightsTargetOwnerType.INBOUND_MESSAGE,
        }:
            target.locator_cleared_at = now


def _remove_patient_linked_handoff_evidence(
    session: Session,
    *,
    clinic_id: str,
    patient_id: str,
    job_ids: tuple[str, ...],
    appointment_ids: tuple[str, ...],
    inbound_call_ids: set[str],
    inbound_message_ids: set[str],
) -> None:
    """Remove unapproved patient-linked receipts only after provider ambiguity settles."""
    escalation_ids = tuple(
        session.execute(
            sa.select(Escalation.id).where(
                Escalation.clinic_id == clinic_id,
                Escalation.patient_id == patient_id,
            )
        ).scalars()
    )
    booking_conditions: list[sa.ColumnElement[bool]] = []
    if appointment_ids:
        booking_conditions.append(BookingAction.appointment_id.in_(appointment_ids))
    if job_ids:
        booking_conditions.append(BookingAction.outreach_job_id.in_(job_ids))
    booking_action_ids: tuple[str, ...] = ()
    if booking_conditions:
        booking_action_ids = tuple(
            session.execute(
                sa.select(BookingAction.id).where(
                    BookingAction.clinic_id == clinic_id,
                    sa.or_(*booking_conditions),
                )
            ).scalars()
        )
    task_filter = sa.or_(
        InboundStaffTask.patient_id == patient_id,
        InboundStaffTask.inbound_call_id.in_(inbound_call_ids)
        if inbound_call_ids
        else sa.false(),
        InboundStaffTask.inbound_message_id.in_(inbound_message_ids)
        if inbound_message_ids
        else sa.false(),
    )
    inbound_task_ids = tuple(
        session.execute(
            sa.select(InboundStaffTask.id).where(
                InboundStaffTask.clinic_id == clinic_id,
                task_filter,
            )
        ).scalars()
    )
    receipt_filter = sa.or_(
        HandoffReceipt.escalation_id.in_(escalation_ids)
        if escalation_ids
        else sa.false(),
        HandoffReceipt.booking_action_id.in_(booking_action_ids)
        if booking_action_ids
        else sa.false(),
        HandoffReceipt.inbound_staff_task_id.in_(inbound_task_ids)
        if inbound_task_ids
        else sa.false(),
    )
    statement = tenant_select(HandoffReceipt).where(receipt_filter)
    if _is_postgresql(session):
        statement = statement.with_for_update()
    receipts = list(session.execute(statement).scalars())
    if not receipts:
        return
    receipt_ids = tuple(receipt.id for receipt in receipts)
    effect_statement = tenant_select(ExternalEffect).where(
        ExternalEffect.effect_type == ExternalEffectType.HANDOFF_NOTIFICATION,
        ExternalEffect.aggregate_type == "handoff_receipt",
        ExternalEffect.aggregate_id.in_(receipt_ids),
    )
    if _is_postgresql(session):
        effect_statement = effect_statement.with_for_update()
    effects = list(session.execute(effect_statement).scalars())
    if any(
        effect.state
        in {ExternalEffectState.DISPATCHING, ExternalEffectState.RECONCILE_REQUIRED}
        for effect in effects
    ):
        raise RightsCompletionBlocked("handoff_notification_unsettled")
    effect_ids = tuple(effect.id for effect in effects)
    if effect_ids:
        handoff_count = int(
            session.scalar(
                sa.select(sa.func.count())
                .select_from(ExternalEffectHandoff)
                .where(
                    ExternalEffectHandoff.clinic_id == clinic_id,
                    ExternalEffectHandoff.external_effect_id.in_(effect_ids),
                )
            )
            or 0
        )
        if handoff_count:
            raise RightsCompletionBlocked("handoff_notification_manual_work")
        session.execute(
            sa.delete(ProviderCallbackReceipt).where(
                ProviderCallbackReceipt.clinic_id == clinic_id,
                ProviderCallbackReceipt.external_effect_id.in_(effect_ids),
            )
        )
        session.execute(
            sa.delete(ExternalEffect).where(
                ExternalEffect.clinic_id == clinic_id,
                ExternalEffect.id.in_(effect_ids),
            )
        )
    session.execute(
        sa.delete(HandoffReceipt).where(
            HandoffReceipt.clinic_id == clinic_id,
            HandoffReceipt.id.in_(receipt_ids),
        )
    )
    session.flush()


def _require_owner_targets_settled(
    targets: list[RightsTarget],
    now: datetime,
) -> None:
    for target in targets:
        if target.owner_type not in {
            RightsTargetOwnerType.EXTERNAL_EFFECT,
            RightsTargetOwnerType.CALL_RECORD,
            RightsTargetOwnerType.INBOUND_CALL,
            RightsTargetOwnerType.INBOUND_MESSAGE,
        }:
            continue
        if target.state == RightsTargetState.VERIFIED:
            continue
        if target.state == RightsTargetState.RESIDUAL and _residual_is_approved(
            target,
            now,
        ):
            continue
        raise RightsCompletionBlocked("unsettled_locator_owner")


def _completion_evidence_hash(
    request: RightsRequest,
    targets: list[RightsTarget],
) -> str:
    encoded = json.dumps(
        {
            "request_id": request.id,
            "state": RightsRequestState.COMPLETED.value,
            "targets": [
                {
                    "category": (
                        target.residual_category.value
                        if target.residual_category is not None
                        else None
                    ),
                    "completion_eligible": target.residual_completion_eligible,
                    "due_at": (
                        _database_utc(target.residual_due_at).isoformat()
                        if target.residual_due_at is not None
                        else None
                    ),
                    "evidence_hash": target.residual_approval_evidence_hash,
                    "policy_version": target.residual_policy_version,
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


def assert_patient_writable(
    session: Session,
    clinic_id: str,
    patient_id: str,
) -> Patient:
    """Lock an active patient and reject any permanent erasure aggregate."""
    with clinic_scope(session, clinic_id):
        patient_statement = tenant_select(Patient).where(Patient.id == patient_id)
        if _is_postgresql(session):
            patient_statement = patient_statement.with_for_update()
        patient = session.execute(patient_statement).scalar_one_or_none()
        if patient is None:
            raise LookupError("patient not found for clinic")
        frozen = session.execute(
            tenant_select(RightsRequest).where(
                RightsRequest.kind == RightsRequestKind.ERASURE,
                RightsRequest.patient_id == patient.id,
            )
        ).first()
        if frozen is not None:
            raise SubjectFrozenError()
        return patient


def assert_source_writable(
    session: Session,
    clinic_id: str,
    source_ref: str,
    keyring: SubjectKeyring,
) -> None:
    """Reject sync for current or rotated permanent subject tombstones.

    Covers both the primary subject identity (``RightsRequest``) and every
    frozen provider alias (``RightsAliasTombstone``), so an erased subject
    cannot be rehydrated through any linked source reference.
    """
    hashes = subject_key_hashes(clinic_id, source_ref, keyring)
    current_hash = _keyed_hash(
        keyring.current,
        "subject",
        _canonical_subject(clinic_id, source_ref),
    )
    with clinic_scope(session, clinic_id):
        _lock_subject_identity(session, current_hash)
        frozen = session.execute(
            tenant_select(RightsRequest).where(
                RightsRequest.kind == RightsRequestKind.ERASURE,
                RightsRequest.subject_key_hash.in_(hashes),
            )
        ).first()
        if frozen is not None:
            raise SubjectFrozenError()
        frozen_alias = session.execute(
            tenant_select(RightsAliasTombstone).where(
                RightsAliasTombstone.subject_key_hash.in_(hashes)
            )
        ).first()
        if frozen_alias is not None:
            raise SubjectFrozenError()


def _patient_job_ids(session: Session, patient_id: str) -> tuple[str, ...]:
    statement = tenant_select(OutreachJob).where(OutreachJob.patient_id == patient_id)
    if _is_postgresql(session):
        statement = statement.with_for_update()
    return tuple(job.id for job in session.execute(statement).scalars())


def _freeze_source_aliases(
    session: Session,
    clinic_id: str,
    patient_id: str,
    request: RightsRequest,
    keyring: SubjectKeyring,
) -> None:
    """Freeze every active provider alias and record permanent tombstones.

    Runs inside the erasure freeze transaction. Tombstones reuse the versioned
    subject-hash vocabulary, carry no raw source reference, reference only the
    permanent rights aggregate, and survive patient and link deletion.
    """
    statement = tenant_select(PatientSourceLink).where(
        PatientSourceLink.patient_id == patient_id,
        PatientSourceLink.state == SourceLinkState.ACTIVE,
    )
    if _is_postgresql(session):
        statement = statement.with_for_update()
    links = list(session.execute(statement).scalars())
    seen_hashes = {request.subject_key_hash}
    for link in links:
        link.state = SourceLinkState.FROZEN
        alias_hash = _keyed_hash(
            keyring.current,
            "subject",
            _canonical_subject(clinic_id, link.source_ref),
        )
        if alias_hash in seen_hashes:
            continue
        seen_hashes.add(alias_hash)
        session.add(
            RightsAliasTombstone(
                id=f"rights-alias-{uuid.uuid4().hex}",
                clinic_id=clinic_id,
                rights_request_id=request.id,
                provider=link.provider,
                subject_key_hash=alias_hash,
                subject_key_version=keyring.current.version,
            )
        )
    if links:
        session.flush()


def _patient_effects(
    session: Session,
    job_ids: tuple[str, ...],
) -> list[ExternalEffect]:
    if not job_ids:
        return []
    booking_action_ids = sa.select(BookingAction.id).where(
        BookingAction.outreach_job_id.in_(job_ids),
    )
    statement = tenant_select(ExternalEffect).where(
        sa.or_(
            sa.and_(
                ExternalEffect.aggregate_type == "outreach_job",
                ExternalEffect.aggregate_id.in_(job_ids),
            ),
            sa.and_(
                ExternalEffect.aggregate_type == "booking_action",
                ExternalEffect.aggregate_id.in_(booking_action_ids),
            ),
        ),
        ExternalEffect.effect_type != ExternalEffectType.RIGHTS,
    )
    if _is_postgresql(session):
        statement = statement.with_for_update()
    return list(session.execute(statement).scalars())


def _patient_call_records(
    session: Session,
    patient_id: str,
    effect_ids: set[str],
) -> list[CallRecord]:
    conditions = [CallRecord.patient_id == patient_id]
    if effect_ids:
        conditions.append(CallRecord.external_effect_id.in_(effect_ids))
    statement = tenant_select(CallRecord).where(sa.or_(*conditions))
    if _is_postgresql(session):
        statement = statement.with_for_update()
    return list(session.execute(statement).scalars())


def _call_record_effects(
    session: Session,
    call_record_ids: set[str],
) -> list[ExternalEffect]:
    if not call_record_ids:
        return []
    statement = tenant_select(ExternalEffect).where(
        ExternalEffect.aggregate_type == "call_record",
        ExternalEffect.aggregate_id.in_(call_record_ids),
        ExternalEffect.effect_type != ExternalEffectType.RIGHTS,
    )
    if _is_postgresql(session):
        statement = statement.with_for_update()
    return list(session.execute(statement).scalars())


def _patient_inbound_owners(
    session: Session,
    patient_id: str,
) -> tuple[list[InboundCall], list[InboundMessage]]:
    tasks = list(
        session.execute(
            tenant_select(InboundStaffTask).where(
                InboundStaffTask.patient_id == patient_id
            )
        ).scalars()
    )
    call_ids = {task.inbound_call_id for task in tasks if task.inbound_call_id}
    message_ids = {
        task.inbound_message_id for task in tasks if task.inbound_message_id
    }
    inbound_calls = (
        list(
            session.execute(
                tenant_select(InboundCall).where(InboundCall.id.in_(call_ids))
            ).scalars()
        )
        if call_ids
        else []
    )
    inbound_messages = (
        list(
            session.execute(
                tenant_select(InboundMessage).where(InboundMessage.id.in_(message_ids))
            ).scalars()
        )
        if message_ids
        else []
    )
    return inbound_calls, inbound_messages


def _patient_incidents(
    session: Session,
    job_ids: tuple[str, ...],
) -> list[IncidentReport]:
    if not job_ids:
        return []
    return list(
        session.execute(
            tenant_select(IncidentReport).where(
                IncidentReport.related_job_id.in_(job_ids)
            )
        ).scalars()
    )


def _merge_effects(
    first: list[ExternalEffect],
    second: list[ExternalEffect],
) -> list[ExternalEffect]:
    effects = {effect.id: effect for effect in first}
    effects.update({effect.id: effect for effect in second})
    return [effects[effect_id] for effect_id in sorted(effects)]


def _target_specs(
    key: SubjectKey,
    effects: list[ExternalEffect],
    call_records: list[CallRecord],
    inbound_calls: list[InboundCall],
    inbound_messages: list[InboundMessage],
    incidents: list[IncidentReport],
    request_id: str,
) -> list[tuple[_TargetSpec, str]]:
    specs: dict[str, _TargetSpec] = {}

    def add(spec: _TargetSpec) -> None:
        target_hash = _keyed_hash(
            key,
            "target",
            json.dumps(
                {
                    "action": spec.action.value,
                    "locator": spec.locator,
                    "resource": spec.resource.value,
                    "system": spec.system.value,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        specs.setdefault(target_hash, spec)

    for record in sorted(call_records, key=lambda item: item.id):
        if record.provider != ClinicPhoneProvider.TWILIO:
            continue
        if _valid_sid(record.provider_call_id, RightsTargetResource.CALL):
            add(
                _TargetSpec(
                    system=RightsTargetSystem.TWILIO,
                    resource=RightsTargetResource.CALL,
                    action=RightsTargetAction.DELETE,
                    owner_type=RightsTargetOwnerType.CALL_RECORD,
                    owner_id=record.id,
                    locator=str(record.provider_call_id),
                )
            )
        if _valid_sid(record.recording_sid, RightsTargetResource.RECORDING):
            recording_sid = str(record.recording_sid)
            recording_dispatch_ready = (
                record.recording_status not in _UNSTABLE_RECORDING_STATES
            )
            add(
                _TargetSpec(
                    system=RightsTargetSystem.TWILIO,
                    resource=RightsTargetResource.TRANSCRIPTION_COLLECTION,
                    action=RightsTargetAction.PURGE,
                    owner_type=RightsTargetOwnerType.CALL_RECORD,
                    owner_id=record.id,
                    locator=recording_sid,
                    dispatch_ready=recording_dispatch_ready,
                )
            )
            add(
                _TargetSpec(
                    system=RightsTargetSystem.TWILIO,
                    resource=RightsTargetResource.RECORDING,
                    action=RightsTargetAction.DELETE,
                    owner_type=RightsTargetOwnerType.CALL_RECORD,
                    owner_id=record.id,
                    locator=recording_sid,
                    dispatch_ready=recording_dispatch_ready,
                )
            )
        if record.recording_blob_path:
            add(
                _TargetSpec(
                    system=RightsTargetSystem.AZURE_BLOB,
                    resource=RightsTargetResource.BLOB_COLLECTION,
                    action=RightsTargetAction.PURGE,
                    owner_type=RightsTargetOwnerType.CALL_RECORD,
                    owner_id=record.id,
                    locator=record.recording_blob_path,
                    dispatch_ready=(
                        record.recording_status not in _UNSTABLE_RECORDING_STATES
                    ),
                )
            )

    for effect in sorted(effects, key=lambda item: item.id):
        if effect.effect_type == ExternalEffectType.SMS and _valid_sid(
            effect.provider_resource_id,
            RightsTargetResource.MESSAGE,
        ):
            add(
                _TargetSpec(
                    system=RightsTargetSystem.TWILIO,
                    resource=RightsTargetResource.MESSAGE,
                    action=RightsTargetAction.DELETE,
                    owner_type=RightsTargetOwnerType.EXTERNAL_EFFECT,
                    owner_id=effect.id,
                    locator=str(effect.provider_resource_id),
                )
            )
        elif effect.effect_type == ExternalEffectType.CALL and _valid_sid(
            effect.provider_resource_id,
            RightsTargetResource.CALL,
        ):
            add(
                _TargetSpec(
                    system=RightsTargetSystem.TWILIO,
                    resource=RightsTargetResource.CALL,
                    action=RightsTargetAction.DELETE,
                    owner_type=RightsTargetOwnerType.EXTERNAL_EFFECT,
                    owner_id=effect.id,
                    locator=str(effect.provider_resource_id),
                )
            )

    for inbound_call in sorted(inbound_calls, key=lambda item: item.id):
        if (
            inbound_call.provider == ClinicPhoneProvider.TWILIO
            and _valid_sid(inbound_call.provider_call_id, RightsTargetResource.CALL)
        ):
            add(
                _TargetSpec(
                    system=RightsTargetSystem.TWILIO,
                    resource=RightsTargetResource.CALL,
                    action=RightsTargetAction.DELETE,
                    owner_type=RightsTargetOwnerType.INBOUND_CALL,
                    owner_id=inbound_call.id,
                    locator=inbound_call.provider_call_id,
                )
            )
    for inbound_message in sorted(inbound_messages, key=lambda item: item.id):
        if (
            inbound_message.provider == ClinicPhoneProvider.TWILIO
            and _valid_sid(
                inbound_message.provider_message_id,
                RightsTargetResource.MESSAGE,
            )
        ):
            add(
                _TargetSpec(
                    system=RightsTargetSystem.TWILIO,
                    resource=RightsTargetResource.MESSAGE,
                    action=RightsTargetAction.DELETE,
                    owner_type=RightsTargetOwnerType.INBOUND_MESSAGE,
                    owner_id=inbound_message.id,
                    locator=inbound_message.provider_message_id,
                )
            )

    for incident in sorted(incidents, key=lambda item: item.id):
        add(
            _TargetSpec(
                system=RightsTargetSystem.LOCAL,
                resource=RightsTargetResource.INCIDENT_RECORD,
                action=RightsTargetAction.PROCEDURE,
                owner_type=RightsTargetOwnerType.INCIDENT_REPORT,
                owner_id=incident.id,
                locator=f"incident:{incident.id}",
                dispatchable=False,
                initial_state=RightsTargetState.RESIDUAL,
                residual_category=RightsResidualCategory.CLINICAL_GOVERNANCE_RECORD,
            )
        )

    add(
        _TargetSpec(
            system=RightsTargetSystem.LOCAL,
            resource=RightsTargetResource.PATIENT_GRAPH,
            action=RightsTargetAction.MINIMIZE,
            owner_type=RightsTargetOwnerType.RIGHTS_REQUEST,
            owner_id=request_id,
            locator="local-patient-graph",
            dispatchable=False,
        )
    )
    procedure_specs = (
        (
            RightsTargetSystem.CONTROLLER,
            RightsTargetResource.CLINIKO,
            RightsResidualCategory.CLINIKO_CONTROLLER_PROCEDURE,
        ),
        (
            RightsTargetSystem.PROCESSOR,
            RightsTargetResource.POSTGRES_BACKUP,
            RightsResidualCategory.POSTGRES_BACKUP_WINDOW,
        ),
        (
            RightsTargetSystem.PROCESSOR,
            RightsTargetResource.APPLICATION_LOG,
            RightsResidualCategory.APPLICATION_LOG_WINDOW,
        ),
        (
            RightsTargetSystem.PROCESSOR,
            RightsTargetResource.MONITOR_LOG,
            RightsResidualCategory.MONITOR_LOG_WINDOW,
        ),
        (
            RightsTargetSystem.PROCESSOR,
            RightsTargetResource.SUPPORT_PATH,
            RightsResidualCategory.SUPPORT_PROCEDURE,
        ),
        (
            RightsTargetSystem.PROCESSOR,
            RightsTargetResource.VOICE_LIVE,
            RightsResidualCategory.VOICE_LIVE_PROCESSOR_PROCEDURE,
        ),
        (
            RightsTargetSystem.PROCESSOR,
            RightsTargetResource.REDIS_SESSION,
            RightsResidualCategory.REDIS_SESSION_PROCEDURE,
        ),
    )
    for system, resource, category in procedure_specs:
        add(
            _TargetSpec(
                system=system,
                resource=resource,
                action=RightsTargetAction.PROCEDURE,
                owner_type=RightsTargetOwnerType.RIGHTS_REQUEST,
                owner_id=request_id,
                locator=f"procedure:{resource.value}",
                dispatchable=False,
                initial_state=RightsTargetState.RESIDUAL,
                residual_category=category,
            )
        )

    return [(specs[target_hash], target_hash) for target_hash in sorted(specs)]


def _cancel_undispatched(
    session: Session,
    effects: list[ExternalEffect],
    call_records: list[CallRecord],
    now: datetime,
) -> None:
    call_records_by_id = {record.id: record for record in call_records}
    booking_action_ids = {
        effect.aggregate_id
        for effect in effects
        if effect.effect_type == ExternalEffectType.CLINIKO_BOOKING
        and effect.aggregate_type == "booking_action"
    }
    booking_actions: dict[str, BookingAction] = {}
    if booking_action_ids:
        statement = tenant_select(BookingAction).where(
            BookingAction.id.in_(booking_action_ids)
        )
        if _is_postgresql(session):
            statement = statement.with_for_update()
        booking_actions = {
            action.id: action for action in session.execute(statement).scalars()
        }
    for effect in effects:
        if (
            effect.effect_type == ExternalEffectType.RECORDING
            and effect.payload.get("intent") == "recording_stop"
        ):
            continue
        if (
            effect.effect_type == ExternalEffectType.CLINIKO_BOOKING
            and effect.state == ExternalEffectState.DISPATCHING
        ):
            effect.state = ExternalEffectState.RECONCILE_REQUIRED
            effect.provider_status = "provider_outcome_unknown"
            effect.last_error_class = "AmbiguousDispatch"
            effect.last_error_code = _CANCELED_REASON
            effect.lease_owner = None
            effect.lease_expires_at = None
            action = booking_actions.get(effect.aggregate_id)
            if action is not None:
                action.write_back_state = BookingWriteBackState.RECONCILE_REQUIRED
            continue
        if effect.state not in {
            ExternalEffectState.PENDING,
            ExternalEffectState.LEASED,
        }:
            continue
        effect.state = ExternalEffectState.CANCELED
        effect.provider_status = "not_dispatched"
        effect.last_error_class = "DispatchCanceled"
        effect.last_error_code = _CANCELED_REASON
        effect.completed_at = now
        effect.lease_owner = None
        effect.lease_expires_at = None
        action = booking_actions.get(effect.aggregate_id)
        if action is not None:
            action.write_back_state = BookingWriteBackState.REJECTED
        if (
            effect.effect_type == ExternalEffectType.RECORDING
            and effect.aggregate_type == "call_record"
            and effect.payload.get("intent") == "recording_start"
        ):
            record = call_records_by_id.get(effect.aggregate_id)
            if record is not None and record.recording_status.value == "start_pending":
                from .enums import CallRecordingStatus

                record.recording_status = CallRecordingStatus.ABSENT


def _inventory_is_final(
    effects: list[ExternalEffect],
    call_records: list[CallRecord],
) -> bool:
    terminal_effect_states = {
        ExternalEffectState.SUCCEEDED,
        ExternalEffectState.REJECTED,
        ExternalEffectState.DEAD_LETTER,
        ExternalEffectState.CANCELED,
    }
    if any(effect.state not in terminal_effect_states for effect in effects):
        return False
    return all(
        record.recording_status not in _UNSTABLE_RECORDING_STATES
        for record in call_records
    )


def _valid_sid(value: str | None, resource: RightsTargetResource) -> bool:
    pattern = _SID_PATTERNS.get(resource)
    return bool(value and pattern and pattern.fullmatch(value))


def _canonical_subject(clinic_id: str, source_ref: str) -> str:
    clinic_id = _required(clinic_id, "clinic_id")
    source_ref = _required(source_ref, "source_ref")
    return json.dumps(
        {"clinic_id": clinic_id, "source_ref": source_ref},
        separators=(",", ":"),
        sort_keys=True,
    )


def _keyed_hash(key: SubjectKey, namespace: str, value: str) -> str:
    message = f"clinic-recall:rights:v1:{namespace}:{value}".encode()
    return hmac.new(key.secret, message, hashlib.sha256).hexdigest()


def _result(request: RightsRequest, *, created: bool) -> RightsRequestResult:
    return RightsRequestResult(
        request_id=request.id,
        state=request.state,
        created=created,
        target_count=request.target_count,
        due_at=request.due_at,
    )


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _required(value: str, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _require_sha256(value: str, name: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _is_postgresql(session: Session) -> bool:
    return bool(session.bind is not None and session.bind.dialect.name == "postgresql")


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _residual_is_approved(target: RightsTarget, now: datetime) -> bool:
    return bool(
        target.residual_category is not None
        and target.residual_policy_version
        and target.residual_approval_evidence_hash
        and target.residual_completion_eligible
        and target.residual_due_at is not None
        and _database_utc(target.residual_due_at) >= now
    )


def _lock_subject_identity(session: Session, subject_key_hash: str) -> None:
    if not _is_postgresql(session):
        return
    unsigned = int(subject_key_hash[:16], 16)
    advisory_key = unsigned if unsigned < 2**63 else unsigned - 2**64
    session.execute(
        sa.text("SELECT pg_advisory_xact_lock(:advisory_key)"),
        {"advisory_key": advisory_key},
    )