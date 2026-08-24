"""Deterministic receipt, severity, and SLA policy for human handoffs."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .enums import (
    AuditAction,
    BookingActionStatus,
    EscalationReason,
    EscalationStatus,
    ExternalEffectState,
    HandoffAlternateState,
    HandoffDeliveryState,
    HandoffSeverity,
    InboundStaffTaskKind,
    InboundStaffTaskStatus,
    PilotProgrammeState,
)
from .messaging.audit import audit_action
from .models import (
    BookingAction,
    Clinic,
    Escalation,
    ExternalEffect,
    ExternalEffectHandoff,
    HandoffReceipt,
    InboundStaffTask,
    PilotProgramme,
)
from .telemetry import queue_after_commit
from .types import (
    DEFAULT_CONTACT_END_HOUR,
    DEFAULT_CONTACT_START_HOUR,
    DEFAULT_TIMEZONE,
)

BUILTIN_HANDOFF_SLA_VERSION = "pilot-handoff-sla-v1"
_VERSION = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_CRITICAL_MAX_MINUTES = 5
_HIGH_MAX_MINUTES = 15
_NORMAL_MAX_BUSINESS_HOURS = 4
_CRITICAL_INBOUND_REASONS = frozenset({"urgent", "safeguarding"})
_HIGH_INBOUND_REASONS = frozenset({"distress", "clinical", "complaint"})
_KNOWN_NORMAL_INBOUND_REASONS = frozenset(
    {
        "ambiguous",
        "booking_request",
        "callback",
        "identity_unclear",
        "opt_out_identity_unclear",
        "semantic_rights_review",
    }
)


@dataclass(frozen=True)
class HandoffSlaPolicy:
    """Immutable SLA values copied onto each receipt."""

    version: str
    canonical_sha256: str
    critical_sla: timedelta
    high_sla: timedelta
    normal_business_hours: int


@dataclass(frozen=True)
class HandoffReceiptResult:
    """Outcome of creating, replaying, or upgrading one owner receipt."""

    receipt: HandoffReceipt
    created: bool
    upgraded: bool
    notification_effect_created: bool
    unknown_reason: bool = False


def built_in_handoff_sla_policy() -> HandoffSlaPolicy:
    """Return the hard pilot ceilings used whenever authority is unavailable."""
    return _make_policy(
        version=BUILTIN_HANDOFF_SLA_VERSION,
        critical_minutes=_CRITICAL_MAX_MINUTES,
        high_minutes=_HIGH_MAX_MINUTES,
        normal_business_hours=_NORMAL_MAX_BUSINESS_HOURS,
    )


def handoff_sla_policy_from_config(
    config: Mapping[str, object] | None,
) -> HandoffSlaPolicy:
    """Accept only a complete, bounded policy that is no looser than the pilot."""
    fallback = built_in_handoff_sla_policy()
    if config is None:
        return fallback
    try:
        version = config["version"]
        critical_minutes = config["critical_minutes"]
        high_minutes = config["high_minutes"]
        normal_business_hours = config["normal_business_hours"]
    except (KeyError, TypeError):
        return fallback
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        return fallback
    values = (critical_minutes, high_minutes, normal_business_hours)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        return fallback
    if not 1 <= critical_minutes <= _CRITICAL_MAX_MINUTES:
        return fallback
    if not 1 <= high_minutes <= _HIGH_MAX_MINUTES:
        return fallback
    if not 1 <= normal_business_hours <= _NORMAL_MAX_BUSINESS_HOURS:
        return fallback
    return _make_policy(
        version=version,
        critical_minutes=critical_minutes,
        high_minutes=high_minutes,
        normal_business_hours=normal_business_hours,
    )


def severity_for_escalation(reason: EscalationReason | str) -> HandoffSeverity:
    """Map the closed outbound escalation reason vocabulary to an SLA severity."""
    resolved = EscalationReason(reason)
    if resolved == EscalationReason.URGENT:
        return HandoffSeverity.CRITICAL
    if resolved in {EscalationReason.CLINICAL, EscalationReason.COMPLAINT}:
        return HandoffSeverity.HIGH
    return HandoffSeverity.NORMAL


def severity_for_inbound_task(
    kind: InboundStaffTaskKind | str,
    reason: str | None,
) -> tuple[HandoffSeverity, bool]:
    """Map deterministic inbound reasons and flag unknown vocabulary."""
    resolved_kind = InboundStaffTaskKind(kind)
    normalized = str(reason or "").strip().lower()
    if resolved_kind == InboundStaffTaskKind.ESCALATION:
        if normalized in _CRITICAL_INBOUND_REASONS:
            return HandoffSeverity.CRITICAL, False
        if normalized in _HIGH_INBOUND_REASONS:
            return HandoffSeverity.HIGH, False
    known = normalized in (
        _CRITICAL_INBOUND_REASONS
        | _HIGH_INBOUND_REASONS
        | _KNOWN_NORMAL_INBOUND_REASONS
    )
    return HandoffSeverity.NORMAL, bool(normalized) and not known


def calculate_handoff_due_at(
    *,
    queued_at: datetime,
    severity: HandoffSeverity | str,
    policy: HandoffSlaPolicy,
    timezone_name: str,
    contact_hours: Mapping[str, object] | None,
) -> datetime:
    """Calculate one immutable aware UTC deadline from trusted clinic settings."""
    _require_aware(queued_at)
    queued_at = queued_at.astimezone(UTC)
    resolved_severity = HandoffSeverity(severity)
    if resolved_severity == HandoffSeverity.CRITICAL:
        return queued_at + policy.critical_sla
    if resolved_severity == HandoffSeverity.HIGH:
        return queued_at + policy.high_sla
    zone = _safe_zone(timezone_name)
    start_hour, end_hour = _safe_contact_window(contact_hours)
    cursor = queued_at.astimezone(zone)
    remaining = timedelta(hours=policy.normal_business_hours)
    while remaining > timedelta(0):
        window_start, window_end = _window(cursor.date(), zone, start_hour, end_hour)
        if cursor < window_start:
            cursor = window_start
        elif cursor >= window_end:
            cursor = _window(
                cursor.date() + timedelta(days=1),
                zone,
                start_hour,
                end_hour,
            )[0]
            continue
        available = window_end - cursor
        consumed = min(available, remaining)
        cursor += consumed
        remaining -= consumed
    return cursor.astimezone(UTC)


def ensure_handoff_receipt(
    session: Session,
    clinic_id: str,
    owner: Escalation | InboundStaffTask | BookingAction | ExternalEffectHandoff,
    *,
    now: datetime,
    policy: HandoffSlaPolicy | None = None,
) -> HandoffReceiptResult:
    """Create or monotonically upgrade one receipt in the caller's transaction."""
    _require_aware(now)
    now = now.astimezone(UTC)
    resolved_policy = policy or built_in_handoff_sla_policy()
    owner_column, owner_id, owner_kind = _owner_binding(owner)
    if owner.clinic_id != clinic_id:
        raise LookupError("handoff owner not found for clinic")
    severity, unknown_reason = _owner_severity(owner)
    with clinic_scope(session, clinic_id):
        clinic = session.execute(
            tenant_select(Clinic).where(Clinic.id == clinic_id)
        ).scalar_one_or_none()
        if clinic is None:
            raise LookupError("clinic not found for handoff receipt")
        existing = _load_receipt(
            session,
            owner_column=owner_column,
            owner_id=owner_id,
            for_update=True,
        )
        if existing is not None:
            return _update_existing_receipt(
                session,
                clinic=clinic,
                receipt=existing,
                severity=severity,
                now=now,
                unknown_reason=unknown_reason,
                owner_kind=owner_kind,
            )
        critical_minutes = _whole_minutes(resolved_policy.critical_sla)
        high_minutes = _whole_minutes(resolved_policy.high_sla)
        due_at = calculate_handoff_due_at(
            queued_at=now,
            severity=severity,
            policy=resolved_policy,
            timezone_name=clinic.timezone,
            contact_hours=clinic.contact_hours,
        )
        owner_values = {
            "escalation_id": None,
            "inbound_staff_task_id": None,
            "booking_action_id": None,
            "external_effect_handoff_id": None,
        }
        owner_values[owner_column] = owner_id
        receipt = HandoffReceipt(
            id=f"handoff-receipt-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            **owner_values,
            severity=severity,
            delivery_state=HandoffDeliveryState.QUEUED,
            queued_at=now,
            due_at=due_at,
            sent_at=None,
            delivered_at=None,
            acknowledged_at=None,
            acknowledged_by=None,
            resolved_at=None,
            resolved_by=None,
            policy_version=resolved_policy.version,
            policy_sha256=resolved_policy.canonical_sha256,
            policy_critical_minutes=critical_minutes,
            policy_high_minutes=high_minutes,
            policy_normal_business_hours=resolved_policy.normal_business_hours,
            severity_generation=0,
            notification_count=0,
            escalation_level=0,
            alternate_state=HandoffAlternateState.NOT_REQUESTED,
            alternate_requested_at=None,
        )
        savepoint = session.begin_nested()
        try:
            session.add(receipt)
            session.flush()
        except IntegrityError:
            savepoint.rollback()
            winner = _load_receipt(
                session,
                owner_column=owner_column,
                owner_id=owner_id,
                for_update=True,
            )
            if winner is None:
                raise
            return _update_existing_receipt(
                session,
                clinic=clinic,
                receipt=winner,
                severity=severity,
                now=now,
                unknown_reason=unknown_reason,
                owner_kind=owner_kind,
            )
        savepoint.commit()
        effect_created = _ensure_notification_effect(session, receipt, now)
        if unknown_reason:
            _queue_unknown_reason(session, owner_kind)
        session.flush()
        return HandoffReceiptResult(
            receipt=receipt,
            created=True,
            upgraded=False,
            notification_effect_created=effect_created,
            unknown_reason=unknown_reason,
        )


def ensure_external_effect_handoff(
    session: Session,
    effect: ExternalEffect,
    *,
    reason_code: str,
    now: datetime,
) -> tuple[ExternalEffectHandoff, bool]:
    """Create/reuse one exhausted-effect owner and ensure its receipt atomically."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason_code):
        raise ValueError("handoff reason_code must be a bounded reason code")
    effect_id = str(getattr(effect, "id", ""))
    clinic_id = str(getattr(effect, "clinic_id", ""))
    if not effect_id or not clinic_id:
        raise ValueError("external effect identity is required")
    with clinic_scope(session, clinic_id):
        persisted_effect = session.execute(
            tenant_select(ExternalEffect).where(ExternalEffect.id == effect_id)
        ).scalar_one_or_none()
        if persisted_effect is None:
            raise LookupError("external effect not found for clinic")
        handoff = session.execute(
            tenant_select(ExternalEffectHandoff).where(
                ExternalEffectHandoff.external_effect_id == effect_id
            )
        ).scalar_one_or_none()
        created = handoff is None
        if handoff is None:
            if persisted_effect.state not in {
                ExternalEffectState.REJECTED,
                ExternalEffectState.DEAD_LETTER,
                ExternalEffectState.RECONCILE_REQUIRED,
            }:
                raise ValueError("external effect is not failed handoff work")
            handoff = ExternalEffectHandoff(
                id=f"effect-handoff-{uuid.uuid4().hex}",
                clinic_id=clinic_id,
                external_effect_id=effect_id,
                status="queued",
                reason_code=reason_code,
            )
            savepoint = session.begin_nested()
            try:
                session.add(handoff)
                session.flush()
            except IntegrityError:
                savepoint.rollback()
                handoff = session.execute(
                    tenant_select(ExternalEffectHandoff).where(
                        ExternalEffectHandoff.external_effect_id == effect_id
                    )
                ).scalar_one()
                created = False
            else:
                savepoint.commit()
        ensure_handoff_receipt(session, clinic_id, handoff, now=now)
        session.flush()
        return handoff, created


def request_alternate_notification(
    session: Session,
    receipt: HandoffReceipt,
    *,
    now: datetime,
    reason_code: str,
) -> bool:
    """Persist one alternate-page request without claiming delivery."""
    _require_aware(now)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason_code):
        raise ValueError("alternate reason_code must be bounded")
    if receipt.alternate_state == HandoffAlternateState.REQUESTED:
        return False
    receipt.alternate_state = HandoffAlternateState.REQUESTED
    receipt.alternate_requested_at = now.astimezone(UTC)
    receipt.escalation_level = 1
    queue_after_commit(
        session,
        "handoff.alternate.requested",
        {
            "severity": receipt.severity.value,
            "reason_code": reason_code,
        },
    )
    session.flush()
    return True


def pause_clinic_programmes_for_handoff(
    session: Session,
    *,
    clinic_id: str,
    now: datetime,
    reason_code: str,
) -> int:
    """Pause every non-closed clinic programme through the PR-13 service."""
    _require_aware(now)
    from .pilot_controls import pause_programme

    with clinic_scope(session, clinic_id):
        statement = tenant_select(PilotProgramme).where(
            PilotProgramme.state != PilotProgrammeState.CLOSED
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        programmes = list(session.execute(statement).scalars())
        paused = 0
        for programme in programmes:
            transitioned = programme.state != PilotProgrammeState.PAUSED
            pause_programme(
                session,
                clinic_id=clinic_id,
                programme_id=programme.id,
                actor="system:handoff",
                reason=reason_code,
                now=now,
            )
            paused += int(transitioned)
        queue_after_commit(
            session,
            "handoff.programme.pause",
            {
                "reason_code": reason_code,
                "outcome": "paused" if paused else "already_stopped_or_absent",
            },
        )
        return paused


def handoff_owner_is_active(session: Session, receipt: HandoffReceipt) -> bool:
    """Return whether the bound owner still requires explicit staff resolution."""
    with clinic_scope(session, receipt.clinic_id):
        return _handoff_owner_is_active_scoped(session, receipt)


def _handoff_owner_is_active_scoped(
    session: Session,
    receipt: HandoffReceipt,
) -> bool:
    if receipt.escalation_id is not None:
        owner = session.execute(
            tenant_select(Escalation).where(Escalation.id == receipt.escalation_id)
        ).scalar_one_or_none()
        return owner is not None and owner.status in {
            EscalationStatus.OPEN,
            EscalationStatus.ACKNOWLEDGED,
        }
    if receipt.inbound_staff_task_id is not None:
        owner = session.execute(
            tenant_select(InboundStaffTask).where(
                InboundStaffTask.id == receipt.inbound_staff_task_id
            )
        ).scalar_one_or_none()
        return owner is not None and owner.status in {
            InboundStaffTaskStatus.OPEN,
            InboundStaffTaskStatus.ACKNOWLEDGED,
        }
    if receipt.booking_action_id is not None:
        owner = session.execute(
            tenant_select(BookingAction).where(
                BookingAction.id == receipt.booking_action_id
            )
        ).scalar_one_or_none()
        return owner is not None and owner.status == BookingActionStatus.PENDING
    if receipt.external_effect_handoff_id is not None:
        owner = session.execute(
            tenant_select(ExternalEffectHandoff).where(
                ExternalEffectHandoff.id == receipt.external_effect_handoff_id
            )
        ).scalar_one_or_none()
        return owner is not None and owner.status in {"queued", "acknowledged"}
    return False


def acknowledge_handoff_owner(
    session: Session,
    *,
    clinic_id: str,
    owner: Escalation | InboundStaffTask | BookingAction | ExternalEffectHandoff,
    actor: str,
    now: datetime,
) -> tuple[HandoffReceipt, bool]:
    """Persist trusted staff ownership without resolving or authorising work."""
    _require_aware(now)
    actor = actor.strip()
    if not actor or len(actor) > 200:
        raise ValueError("handoff acknowledgement actor is invalid")
    owner_column, owner_id, owner_kind = _owner_binding(owner)
    if owner.clinic_id != clinic_id:
        raise LookupError("handoff owner not found for clinic")
    if isinstance(owner, Escalation) and owner.status in {
        EscalationStatus.RESOLVED,
        EscalationStatus.CANCELLED,
    }:
        raise ValueError("closed queue item cannot be acknowledged")
    if isinstance(owner, InboundStaffTask) and owner.status in {
        InboundStaffTaskStatus.RESOLVED,
        InboundStaffTaskStatus.CANCELLED,
    }:
        raise ValueError("closed inbound task cannot be acknowledged")
    if isinstance(owner, ExternalEffectHandoff) and owner.status == "resolved":
        raise ValueError("resolved handoff cannot be acknowledged")
    with clinic_scope(session, clinic_id):
        receipt = _load_receipt(
            session,
            owner_column=owner_column,
            owner_id=owner_id,
            for_update=True,
        )
        if receipt is None:
            receipt = ensure_handoff_receipt(
                session,
                clinic_id,
                owner,
                now=now,
            ).receipt
        if receipt.resolved_at is not None:
            raise ValueError("resolved handoff cannot be acknowledged")
        transitioned = receipt.acknowledged_at is None
        if transitioned:
            receipt.acknowledged_at = now.astimezone(UTC)
            receipt.acknowledged_by = actor
            if isinstance(owner, Escalation):
                owner.status = EscalationStatus.ACKNOWLEDGED
                owner.assigned_to = actor
            elif isinstance(owner, InboundStaffTask):
                owner.status = InboundStaffTaskStatus.ACKNOWLEDGED
                owner.assigned_to = actor
            elif isinstance(owner, ExternalEffectHandoff):
                owner.status = "acknowledged"
            audit_action(
                session,
                clinic_id,
                AuditAction.ACKNOWLEDGE,
                owner_id,
                {
                    "owner_kind": owner_kind,
                    "acknowledged_by": actor,
                    "occurred_at": now.astimezone(UTC),
                },
                actor=actor,
            )
            session.flush()
        return receipt, transitioned


def mark_handoff_resolved(
    session: Session,
    receipt: HandoffReceipt,
    *,
    actor: str,
    now: datetime,
) -> bool:
    """Record resolution only after acknowledgement evidence exists."""
    _require_aware(now)
    actor = actor.strip()
    if not actor or len(actor) > 200:
        raise ValueError("handoff resolution actor is invalid")
    if receipt.acknowledged_at is None or receipt.acknowledged_by is None:
        raise ValueError("handoff must be acknowledged before resolution")
    if receipt.resolved_at is not None:
        return False
    resolved_at = now.astimezone(UTC)
    if resolved_at < _as_utc(receipt.acknowledged_at):
        raise ValueError("handoff resolution cannot predate acknowledgement")
    receipt.resolved_at = resolved_at
    receipt.resolved_by = actor
    session.flush()
    return True


def _update_existing_receipt(
    session: Session,
    *,
    clinic: Clinic,
    receipt: HandoffReceipt,
    severity: HandoffSeverity,
    now: datetime,
    unknown_reason: bool,
    owner_kind: str,
) -> HandoffReceiptResult:
    upgraded = _severity_rank(severity) > _severity_rank(receipt.severity)
    if upgraded:
        receipt.severity = severity
        policy = HandoffSlaPolicy(
            version=receipt.policy_version,
            canonical_sha256=receipt.policy_sha256,
            critical_sla=timedelta(minutes=receipt.policy_critical_minutes),
            high_sla=timedelta(minutes=receipt.policy_high_minutes),
            normal_business_hours=receipt.policy_normal_business_hours,
        )
        candidate_due = calculate_handoff_due_at(
            queued_at=_as_utc(receipt.queued_at),
            severity=severity,
            policy=policy,
            timezone_name=clinic.timezone,
            contact_hours=clinic.contact_hours,
        )
        receipt.due_at = min(_as_utc(receipt.due_at), candidate_due)
        if receipt.acknowledged_at is None and receipt.resolved_at is None:
            receipt.severity_generation += 1
    effect_created = False
    if receipt.acknowledged_at is None and receipt.resolved_at is None:
        effect_created = _ensure_notification_effect(session, receipt, now)
    if unknown_reason and upgraded:
        _queue_unknown_reason(session, owner_kind)
    session.flush()
    return HandoffReceiptResult(
        receipt=receipt,
        created=False,
        upgraded=upgraded,
        notification_effect_created=effect_created,
        unknown_reason=unknown_reason,
    )


def _ensure_notification_effect(
    session: Session,
    receipt: HandoffReceipt,
    now: datetime,
) -> bool:
    from .durable.enqueue import enqueue_handoff_notification_effect

    _effect, created = enqueue_handoff_notification_effect(
        session,
        clinic_id=receipt.clinic_id,
        receipt_id=receipt.id,
        severity_generation=receipt.severity_generation,
        available_at=now,
    )
    if created:
        receipt.notification_count += 1
    return created


def _load_receipt(
    session: Session,
    *,
    owner_column: str,
    owner_id: str,
    for_update: bool,
) -> HandoffReceipt | None:
    column = getattr(HandoffReceipt, owner_column)
    statement = tenant_select(HandoffReceipt).where(column == owner_id)
    if (
        for_update
        and session.bind is not None
        and session.bind.dialect.name == "postgresql"
    ):
        statement = statement.with_for_update()
    return session.execute(statement).scalar_one_or_none()


def _owner_binding(
    owner: Escalation | InboundStaffTask | BookingAction | ExternalEffectHandoff,
) -> tuple[str, str, str]:
    if isinstance(owner, Escalation):
        return "escalation_id", owner.id, "escalation"
    if isinstance(owner, InboundStaffTask):
        return "inbound_staff_task_id", owner.id, "inbound_staff_task"
    if isinstance(owner, BookingAction):
        if owner.status != BookingActionStatus.PENDING:
            raise ValueError("only pending booking actions require a handoff receipt")
        return "booking_action_id", owner.id, "booking_action"
    if isinstance(owner, ExternalEffectHandoff):
        return "external_effect_handoff_id", owner.id, "external_effect_handoff"
    raise TypeError("unsupported handoff owner")


def _owner_severity(
    owner: Escalation | InboundStaffTask | BookingAction | ExternalEffectHandoff,
) -> tuple[HandoffSeverity, bool]:
    if isinstance(owner, Escalation):
        return severity_for_escalation(owner.reason), False
    if isinstance(owner, InboundStaffTask):
        return severity_for_inbound_task(owner.kind, owner.reason)
    return HandoffSeverity.NORMAL, False


def _severity_rank(value: HandoffSeverity | str) -> int:
    return {
        HandoffSeverity.NORMAL: 0,
        HandoffSeverity.HIGH: 1,
        HandoffSeverity.CRITICAL: 2,
    }[HandoffSeverity(value)]


def _whole_minutes(value: timedelta) -> int:
    seconds = value.total_seconds()
    if seconds <= 0 or seconds % 60:
        raise ValueError("handoff SLA minutes must be positive whole minutes")
    return int(seconds // 60)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _queue_unknown_reason(session: Session, owner_kind: str) -> None:
    queue_after_commit(
        session,
        "handoff.unknown_reason",
        {"owner_kind": owner_kind},
    )


def _make_policy(
    *,
    version: str,
    critical_minutes: int,
    high_minutes: int,
    normal_business_hours: int,
) -> HandoffSlaPolicy:
    canonical = json.dumps(
        {
            "critical_minutes": critical_minutes,
            "high_minutes": high_minutes,
            "normal_business_hours": normal_business_hours,
            "version": version,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return HandoffSlaPolicy(
        version=version,
        canonical_sha256=hashlib.sha256(canonical).hexdigest(),
        critical_sla=timedelta(minutes=critical_minutes),
        high_sla=timedelta(minutes=high_minutes),
        normal_business_hours=normal_business_hours,
    )


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("queued_at must be timezone-aware")


def _safe_zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def _safe_contact_window(
    contact_hours: Mapping[str, object] | None,
) -> tuple[int, int]:
    values = contact_hours or {}
    start = values.get("start_hour", DEFAULT_CONTACT_START_HOUR)
    end = values.get("end_hour", DEFAULT_CONTACT_END_HOUR)
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start < end <= 24
    ):
        return DEFAULT_CONTACT_START_HOUR, DEFAULT_CONTACT_END_HOUR
    return start, end


def _window(
    day: date,
    zone: ZoneInfo,
    start_hour: int,
    end_hour: int,
) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(hour=start_hour), tzinfo=zone)
    if end_hour == 24:
        end = datetime.combine(day + timedelta(days=1), time(), tzinfo=zone)
    else:
        end = datetime.combine(day, time(hour=end_hour), tzinfo=zone)
    return start, end