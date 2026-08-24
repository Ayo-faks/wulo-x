"""Anonymous-capable staff tasks for inbound clinic contacts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import clinic_scope, tenant_select
from .enums import AuditAction, InboundStaffTaskKind, InboundStaffTaskStatus
from .handoffs import ensure_handoff_receipt
from .messaging.audit import audit_action
from .models import InboundCall, InboundMessage, InboundStaffTask, Patient
from .rights import assert_patient_writable


@dataclass(frozen=True)
class InboundStaffTaskResult:
    """Result of getting, creating, or upgrading an inbound staff task."""

    task_id: str
    kind: InboundStaffTaskKind
    status: InboundStaffTaskStatus
    priority: str
    created: bool
    idempotent: bool
    upgraded: bool


_ACTIVE_STATUSES = {
    InboundStaffTaskStatus.OPEN,
    InboundStaffTaskStatus.ACKNOWLEDGED,
}
_PRIORITY_RANK = {"low": 1, "normal": 2, "high": 3}
_REASON_RANK = {
    "urgent": 9,
    "safeguarding": 8,
    "distress": 7,
    "clinical": 6,
    "complaint": 5,
    "semantic_rights_review": 4,
    "opt_out_identity_unclear": 4,
    "ambiguous": 3,
    "identity_unclear": 2,
}


def create_inbound_staff_task(
    session: Session,
    clinic_id: str,
    *,
    inbound_call_id: str | None = None,
    inbound_message_id: str | None = None,
    kind: InboundStaffTaskKind | str,
    now: datetime,
    priority: str = "normal",
    reason: str | None = None,
    summary: str | None = None,
    payload: dict[str, Any] | None = None,
    patient_id: str | None = None,
) -> InboundStaffTaskResult:
    """Get, create, or upgrade one active scoped inbound human task."""
    _require_aware("now", now)
    resolved_kind = InboundStaffTaskKind(kind)
    if bool(inbound_call_id) == bool(inbound_message_id):
        raise ValueError("exactly one inbound call or message anchor is required")
    with clinic_scope(session, clinic_id):
        call = None
        message = None
        if inbound_call_id:
            call = session.execute(
                tenant_select(InboundCall).where(InboundCall.id == inbound_call_id)
            ).scalar_one_or_none()
            if call is None:
                raise LookupError("inbound call not found for trusted clinic context")
        if inbound_message_id:
            message = session.execute(
                tenant_select(InboundMessage).where(InboundMessage.id == inbound_message_id)
            ).scalar_one_or_none()
            if message is None:
                raise LookupError("inbound message not found for trusted clinic context")
        if patient_id:
            patient = session.execute(
                tenant_select(Patient).where(Patient.id == patient_id)
            ).scalar_one_or_none()
            if patient is None:
                raise LookupError("patient not found for trusted clinic context")
            assert_patient_writable(session, clinic_id, patient.id)
        existing = _find_active_task(
            session,
            inbound_call_id=inbound_call_id,
            inbound_message_id=inbound_message_id,
            kind=resolved_kind,
        )
        if existing is not None:
            if existing.patient_id is not None:
                assert_patient_writable(session, clinic_id, existing.patient_id)
            result = _reuse_or_upgrade_task(
                session,
                existing,
                priority=priority,
                reason=reason,
                summary=summary,
                payload=payload,
                patient_id=patient_id,
            )
            ensure_handoff_receipt(session, clinic_id, existing, now=now)
            return result
        task = InboundStaffTask(
            id=f"inbound-task-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            inbound_call_id=call.id if call else None,
            inbound_message_id=message.id if message else None,
            patient_id=patient_id,
            kind=resolved_kind,
            status=InboundStaffTaskStatus.OPEN,
            priority=_bounded_priority(priority),
            reason=(reason or "")[:128] or None,
            summary=(summary or "")[:1000] or None,
            payload=dict(payload or {}),
        )
        try:
            with session.begin_nested():
                session.add(task)
                audit_action(
                    session,
                    clinic_id,
                    AuditAction.ESCALATE
                    if resolved_kind == InboundStaffTaskKind.ESCALATION
                    else AuditAction.PLACE_CALL,
                    task.id,
                    {
                        "inbound_call_id": call.id if call else None,
                        "inbound_message_id": message.id if message else None,
                        "kind": resolved_kind.value,
                        "priority": task.priority,
                        "reason": task.reason,
                        "occurred_at": now,
                    },
                    actor="system:inbound-assistant",
                )
                session.flush()
                ensure_handoff_receipt(session, clinic_id, task, now=now)
        except IntegrityError:
            winner = _find_active_task(
                session,
                inbound_call_id=inbound_call_id,
                inbound_message_id=inbound_message_id,
                kind=resolved_kind,
            )
            if winner is None:
                raise
            result = _reuse_or_upgrade_task(
                session,
                winner,
                priority=priority,
                reason=reason,
                summary=summary,
                payload=payload,
                patient_id=patient_id,
            )
            ensure_handoff_receipt(session, clinic_id, winner, now=now)
            return result
        return InboundStaffTaskResult(
            task_id=task.id,
            kind=task.kind,
            status=task.status,
            priority=task.priority,
            created=True,
            idempotent=False,
            upgraded=False,
        )


def _find_active_task(
    session: Session,
    *,
    inbound_call_id: str | None,
    inbound_message_id: str | None,
    kind: InboundStaffTaskKind,
) -> InboundStaffTask | None:
    statement = tenant_select(InboundStaffTask).where(
        InboundStaffTask.kind == kind,
        InboundStaffTask.status.in_(_ACTIVE_STATUSES),
    )
    if inbound_call_id:
        statement = statement.where(
            InboundStaffTask.inbound_call_id == inbound_call_id
        )
    else:
        statement = statement.where(
            InboundStaffTask.inbound_message_id == inbound_message_id
        )
    return session.execute(
        statement.order_by(
            InboundStaffTask.created_at,
            InboundStaffTask.id,
        ).limit(1)
    ).scalars().first()


def _reuse_or_upgrade_task(
    session: Session,
    task: InboundStaffTask,
    *,
    priority: str,
    reason: str | None,
    summary: str | None,
    payload: dict[str, Any] | None,
    patient_id: str | None,
) -> InboundStaffTaskResult:
    incoming_priority = _bounded_priority(priority)
    upgraded = False
    if (
        _PRIORITY_RANK[incoming_priority]
        > _PRIORITY_RANK[_bounded_priority(task.priority)]
    ):
        task.priority = incoming_priority
        upgraded = True
    if reason and _reason_rank(reason) > _reason_rank(task.reason):
        task.reason = reason[:128]
        upgraded = True
    if patient_id and task.patient_id is None:
        task.patient_id = patient_id
        upgraded = True
    if upgraded:
        if summary:
            task.summary = summary[:1000]
        if payload:
            task.payload = {**(task.payload or {}), **payload}
        session.flush()
    return InboundStaffTaskResult(
        task_id=task.id,
        kind=task.kind,
        status=task.status,
        priority=task.priority,
        created=False,
        idempotent=True,
        upgraded=upgraded,
    )


def _reason_rank(value: str | None) -> int:
    return _REASON_RANK.get(str(value or "").strip().lower(), 1 if value else 0)


def _bounded_priority(value: str) -> str:
    priority = str(value or "normal").strip().lower()
    return priority if priority in {"low", "normal", "high"} else "normal"


def _require_aware(field: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")