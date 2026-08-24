"""Focused contracts for PR-12 handoff receipts and SLA policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from src.clinic_recall.db import clinic_scope
from src.clinic_recall.enums import (
    EscalationReason,
    EscalationStatus,
    ExternalEffectState,
    ExternalEffectType,
    HandoffSeverity,
    InboundStaffTaskKind,
)
from src.clinic_recall.handoffs import (
    BUILTIN_HANDOFF_SLA_VERSION,
    acknowledge_handoff_owner,
    built_in_handoff_sla_policy,
    calculate_handoff_due_at,
    ensure_external_effect_handoff,
    ensure_handoff_receipt,
    handoff_sla_policy_from_config,
    mark_handoff_resolved,
    severity_for_escalation,
    severity_for_inbound_task,
)
from src.clinic_recall.models import (
    Clinic,
    Escalation,
    ExternalEffect,
    HandoffReceipt,
    Patient,
)
from src.clinic_recall.rights import (
    RightsCompletionBlocked,
    _remove_patient_linked_handoff_evidence,
)


def test_builtin_policy_uses_locked_elapsed_ceiling() -> None:
    policy = built_in_handoff_sla_policy()
    queued_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

    assert policy.version == BUILTIN_HANDOFF_SLA_VERSION
    assert len(policy.canonical_sha256) == 64
    assert calculate_handoff_due_at(
        queued_at=queued_at,
        severity=HandoffSeverity.CRITICAL,
        policy=policy,
        timezone_name="Europe/London",
        contact_hours={"start_hour": 9, "end_hour": 17},
    ) == queued_at + timedelta(minutes=5)
    assert calculate_handoff_due_at(
        queued_at=queued_at,
        severity=HandoffSeverity.HIGH,
        policy=policy,
        timezone_name="Europe/London",
        contact_hours={"start_hour": 9, "end_hour": 17},
    ) == queued_at + timedelta(minutes=15)


def test_normal_sla_accumulates_clinic_window_on_every_calendar_day() -> None:
    policy = built_in_handoff_sla_policy()
    queued_at = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)  # Sunday 19:00 BST.

    due_at = calculate_handoff_due_at(
        queued_at=queued_at,
        severity=HandoffSeverity.NORMAL,
        policy=policy,
        timezone_name="Europe/London",
        contact_hours={"start_hour": 8, "end_hour": 20},
    )

    assert due_at == datetime(2026, 7, 27, 10, 0, tzinfo=UTC)


def test_closed_severity_mapping_does_not_promote_unknown_inbound_reason() -> None:
    assert severity_for_escalation(EscalationReason.URGENT) == HandoffSeverity.CRITICAL
    assert severity_for_escalation(EscalationReason.CLINICAL) == HandoffSeverity.HIGH
    assert severity_for_escalation(EscalationReason.FAILED_CONTACT) == HandoffSeverity.NORMAL

    severity, unknown = severity_for_inbound_task(
        InboundStaffTaskKind.ESCALATION,
        "future_unrecognised_reason",
    )
    assert severity == HandoffSeverity.NORMAL
    assert unknown is True


def test_untrusted_or_looser_override_falls_back_to_hard_policy() -> None:
    built_in = built_in_handoff_sla_policy()
    looser = handoff_sla_policy_from_config(
        {
            "version": "clinic-loose-v1",
            "critical_minutes": 6,
            "high_minutes": 20,
            "normal_business_hours": 5,
        }
    )
    malformed = handoff_sla_policy_from_config({"version": "bad version"})
    stricter = handoff_sla_policy_from_config(
        {
            "version": "clinic-strict-v1",
            "critical_minutes": 4,
            "high_minutes": 10,
            "normal_business_hours": 3,
        }
    )

    assert looser == built_in
    assert malformed == built_in
    assert stricter.version == "clinic-strict-v1"
    assert stricter.critical_sla == timedelta(minutes=4)
    assert stricter.high_sla == timedelta(minutes=10)
    assert stricter.normal_business_hours == 3
    assert stricter.canonical_sha256 != built_in.canonical_sha256


def test_receipt_and_minimized_notification_effect_are_idempotent_and_upgrade_once(
    sqlite_session,
) -> None:
    queued_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    sqlite_session.add(
        Clinic(
            id="clinic-pr12",
            name="PR12",
            timezone="Europe/London",
            contact_hours={"start_hour": 8, "end_hour": 20},
        )
    )
    sqlite_session.add(
        Patient(
            id="patient-pr12",
            clinic_id="clinic-pr12",
            source_ref="P-PR12",
            name="Synthetic",
        )
    )
    escalation = Escalation(
        id="escalation-pr12",
        clinic_id="clinic-pr12",
        patient_id="patient-pr12",
        reason=EscalationReason.AMBIGUOUS,
        status=EscalationStatus.OPEN,
    )
    sqlite_session.add(escalation)
    sqlite_session.flush()

    created = ensure_handoff_receipt(
        sqlite_session,
        "clinic-pr12",
        escalation,
        now=queued_at,
    )
    replayed = ensure_handoff_receipt(
        sqlite_session,
        "clinic-pr12",
        escalation,
        now=queued_at + timedelta(minutes=1),
    )

    assert created.created is True
    assert created.notification_effect_created is True
    assert replayed.created is False
    assert replayed.notification_effect_created is False
    assert replayed.receipt.id == created.receipt.id
    assert created.receipt.severity == HandoffSeverity.NORMAL
    assert created.receipt.queued_at == queued_at
    effects = list(
        sqlite_session.execute(
            select(ExternalEffect).where(
                ExternalEffect.effect_type == ExternalEffectType.HANDOFF_NOTIFICATION
            )
        ).scalars()
    )
    assert len(effects) == 1
    assert effects[0].aggregate_type == "handoff_receipt"
    assert effects[0].aggregate_id == created.receipt.id
    assert effects[0].idempotency_key == (
        f"handoff-notification:{created.receipt.id}:clinic_operations:0:v1"
    )
    assert effects[0].payload == {
        "destination_role": "clinic_operations",
        "receipt_id": created.receipt.id,
        "route_kind": "operational_email",
        "template_version": "handoff-v1",
    }

    escalation.reason = EscalationReason.URGENT
    upgraded = ensure_handoff_receipt(
        sqlite_session,
        "clinic-pr12",
        escalation,
        now=queued_at + timedelta(minutes=2),
    )
    sqlite_session.flush()

    assert upgraded.upgraded is True
    assert upgraded.notification_effect_created is True
    assert upgraded.receipt.severity == HandoffSeverity.CRITICAL
    assert upgraded.receipt.queued_at == queued_at
    assert upgraded.receipt.due_at == queued_at + timedelta(minutes=5)
    assert upgraded.receipt.severity_generation == 1
    assert upgraded.receipt.notification_count == 2
    assert sqlite_session.scalar(
        select(sa.func.count()).select_from(HandoffReceipt)
    ) == 1
    assert sqlite_session.scalar(
        select(sa.func.count())
        .select_from(ExternalEffect)
        .where(ExternalEffect.effect_type == ExternalEffectType.HANDOFF_NOTIFICATION)
    ) == 2


def test_resolution_cannot_predate_acknowledgement(sqlite_session) -> None:
    queued_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    sqlite_session.add(Clinic(id="clinic-order", name="PR12 order"))
    sqlite_session.add(
        Patient(
            id="patient-order",
            clinic_id="clinic-order",
            source_ref="P-ORDER",
            name="Synthetic",
        )
    )
    owner = Escalation(
        id="escalation-order",
        clinic_id="clinic-order",
        patient_id="patient-order",
        reason=EscalationReason.AMBIGUOUS,
        status=EscalationStatus.OPEN,
    )
    sqlite_session.add(owner)
    sqlite_session.flush()
    receipt = ensure_handoff_receipt(
        sqlite_session,
        "clinic-order",
        owner,
        now=queued_at,
    ).receipt
    acknowledge_handoff_owner(
        sqlite_session,
        clinic_id="clinic-order",
        owner=owner,
        actor="staff:test",
        now=queued_at + timedelta(minutes=10),
    )

    with pytest.raises(ValueError, match="cannot predate acknowledgement"):
        mark_handoff_resolved(
            sqlite_session,
            receipt,
            actor="staff:test",
            now=queued_at + timedelta(minutes=5),
        )

    assert receipt.resolved_at is None


def test_pending_external_effect_cannot_create_staff_handoff(sqlite_session) -> None:
    queued_at = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    sqlite_session.add(Clinic(id="clinic-effect-state", name="PR12 effect state"))
    effect = ExternalEffect(
        id="effect-not-failed",
        clinic_id="clinic-effect-state",
        aggregate_type="synthetic",
        aggregate_id="aggregate-not-failed",
        effect_type=ExternalEffectType.SMS,
        idempotency_key="synthetic-not-failed",
        payload_version=1,
        payload={"intent": "synthetic"},
        request_hash="a" * 64,
        state=ExternalEffectState.PENDING,
        available_at=queued_at,
    )
    sqlite_session.add(effect)
    sqlite_session.flush()

    with pytest.raises(ValueError, match="not failed handoff work"):
        ensure_external_effect_handoff(
            sqlite_session,
            effect,
            reason_code="invalid_pending_effect",
            now=queued_at,
        )


def _seed_patient_linked_receipt(sqlite_session) -> tuple[HandoffReceipt, ExternalEffect]:
    sqlite_session.add(
        Clinic(
            id="clinic-rights-pr12",
            name="PR12 rights",
            timezone="Europe/London",
        )
    )
    sqlite_session.add(
        Patient(
            id="patient-rights-pr12",
            clinic_id="clinic-rights-pr12",
            source_ref="P-RIGHTS-PR12",
            name="Synthetic",
        )
    )
    owner = Escalation(
        id="escalation-rights-pr12",
        clinic_id="clinic-rights-pr12",
        patient_id="patient-rights-pr12",
        reason=EscalationReason.CLINICAL,
        status=EscalationStatus.OPEN,
    )
    sqlite_session.add(owner)
    sqlite_session.flush()
    receipt = ensure_handoff_receipt(
        sqlite_session,
        "clinic-rights-pr12",
        owner,
        now=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    ).receipt
    effect = sqlite_session.scalar(
        select(ExternalEffect).where(
            ExternalEffect.aggregate_id == receipt.id,
            ExternalEffect.effect_type == ExternalEffectType.HANDOFF_NOTIFICATION,
        )
    )
    assert effect is not None
    return receipt, effect


def test_rights_removes_settled_patient_linked_receipt_before_owner(
    sqlite_session,
) -> None:
    receipt, effect = _seed_patient_linked_receipt(sqlite_session)

    with clinic_scope(sqlite_session, "clinic-rights-pr12"):
        _remove_patient_linked_handoff_evidence(
            sqlite_session,
            clinic_id="clinic-rights-pr12",
            patient_id="patient-rights-pr12",
            job_ids=(),
            appointment_ids=(),
            inbound_call_ids=set(),
            inbound_message_ids=set(),
        )

    assert sqlite_session.get(HandoffReceipt, receipt.id) is None
    assert sqlite_session.get(ExternalEffect, effect.id) is None
    assert sqlite_session.get(Escalation, "escalation-rights-pr12") is not None


@pytest.mark.parametrize(
    "effect_state",
    [ExternalEffectState.DISPATCHING, ExternalEffectState.RECONCILE_REQUIRED],
)
def test_rights_preserves_unsettled_handoff_notification_until_secured(
    sqlite_session,
    effect_state: ExternalEffectState,
) -> None:
    receipt, effect = _seed_patient_linked_receipt(sqlite_session)
    effect.state = effect_state
    sqlite_session.flush()

    with pytest.raises(
        RightsCompletionBlocked,
        match="handoff_notification_unsettled",
    ):
        with clinic_scope(sqlite_session, "clinic-rights-pr12"):
            _remove_patient_linked_handoff_evidence(
                sqlite_session,
                clinic_id="clinic-rights-pr12",
                patient_id="patient-rights-pr12",
                job_ids=(),
                appointment_ids=(),
                inbound_call_ids=set(),
                inbound_message_ids=set(),
            )

    assert sqlite_session.get(HandoffReceipt, receipt.id) is not None
    assert sqlite_session.get(ExternalEffect, effect.id) is not None
    assert sqlite_session.get(Escalation, "escalation-rights-pr12") is not None