"""Tests for Phase 5 GDPR erasure and retention services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from src.clinic_recall.durable.enqueue import (
    enqueue_call_effect,
    enqueue_recording_start_effect,
    enqueue_sms_effect,
)
from src.clinic_recall.enums import (
    AppointmentStatus,
    AuditAction,
    BookingActionStatus,
    BookingActionType,
    CallRecordingStatus,
    CampaignStatus,
    CampaignType,
    Channel,
    ClinicPhoneProvider,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    ExternalEffectState,
    ExternalEffectType,
    InboundCallStatus,
    InboundMessageStatus,
    InboundStaffTaskKind,
    InteractionDirection,
    InteractionOutcome,
    OutreachState,
    RightsRequestKind,
    RightsResidualCategory,
    RightsTargetOwnerType,
    RightsTargetResource,
    RightsTargetSystem,
)
from src.clinic_recall.erasure import erase_patient, erasure_confirm_token
from src.clinic_recall.models import (
    Appointment,
    AuditLog,
    BookingAction,
    CallRecord,
    Campaign,
    Clinic,
    Escalation,
    ExternalEffect,
    InboundCall,
    InboundMessage,
    InboundStaffTask,
    Interaction,
    OutreachJob,
    Patient,
    RightsRequest,
    RightsTarget,
)
from src.clinic_recall.retention import RetentionPolicy, schedule_retention_requests
from src.clinic_recall.rights import (
    ResidualApproval,
    RightsCompletionBlocked,
    RightsPolicy,
    SubjectFrozenError,
    SubjectKey,
    SubjectKeyring,
    apply_residual_approvals,
    complete_patient_erasure,
    finalize_ready_patient_erasures,
    get_rights_operations_status,
    get_rights_request_status,
    maintain_residual_targets,
    request_patient_erasure,
)

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
MESSAGE_SID = "SM" + "1" * 32
CALL_SID = "CA" + "2" * 32
RECORDING_SID = "RE" + "3" * 32
BLOB_PATH = "clinic-gdpr/calls/recording.mp3"
INBOUND_CALL_SID = "CA" + "4" * 32
INBOUND_MESSAGE_SID = "SM" + "5" * 32
TEST_KEYRING = SubjectKeyring(
    current=SubjectKey(version="tests-only-v1", secret=b"tests-only-rights-hmac-key"),
)
TEST_POLICY = RightsPolicy(
    version="tests-only-policy-v1",
    approval_evidence_hash="a" * 64,
    request_due_after=timedelta(days=28),
)


def _seed_patient_graph(sqlite_session, clinic_id: str = "clinic-gdpr") -> str:
    sqlite_session.add(
        Clinic(
            id=clinic_id,
            name=f"{clinic_id} Clinic",
            timezone="Europe/London",
            consent_policy={"retention_days": 30},
        )
    )
    sqlite_session.add(
        Patient(
            id=f"patient-{clinic_id}",
            clinic_id=clinic_id,
            source_ref=f"P-{clinic_id}",
            name="GDPR Patient",
            phone="+447700930001",
            email="gdpr@example.test",
            consent_flags={"sms": True, "email": True, "call": True},
            opt_out_flags={},
        )
    )
    sqlite_session.add(
        Appointment(
            id=f"appointment-{clinic_id}",
            clinic_id=clinic_id,
            patient_id=f"patient-{clinic_id}",
            source_ref=f"A-{clinic_id}",
            status=AppointmentStatus.MISSED,
            start_at=NOW - timedelta(days=7),
        )
    )
    sqlite_session.add(
        Campaign(
            id=f"campaign-{clinic_id}",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        OutreachJob(
            id=f"job-{clinic_id}",
            clinic_id=clinic_id,
            campaign_id=f"campaign-{clinic_id}",
            patient_id=f"patient-{clinic_id}",
            appointment_id=f"appointment-{clinic_id}",
            channel=Channel.SMS,
            state=OutreachState.SENT,
        )
    )
    sqlite_session.add(
        Interaction(
            id=f"interaction-{clinic_id}",
            clinic_id=clinic_id,
            outreach_job_id=f"job-{clinic_id}",
            channel=Channel.SMS,
            direction=InteractionDirection.INBOUND,
            content="patient free text with PII",
            outcome=InteractionOutcome.ROUTED_TO_STAFF,
            occurred_at=NOW - timedelta(days=40),
        )
    )
    sqlite_session.add(
        BookingAction(
            id=f"booking-{clinic_id}",
            clinic_id=clinic_id,
            appointment_id=f"appointment-{clinic_id}",
            outreach_job_id=f"job-{clinic_id}",
            type=BookingActionType.BOOK,
            status=BookingActionStatus.PENDING,
        )
    )
    sqlite_session.add(
        Escalation(
            id=f"escalation-{clinic_id}",
            clinic_id=clinic_id,
            patient_id=f"patient-{clinic_id}",
            reason=EscalationReason.CLINICAL,
            priority=EscalationPriority.HIGH,
            context_ref=f"interaction-{clinic_id}",
            status=EscalationStatus.OPEN,
        )
    )
    sqlite_session.add(
        AuditLog(
            id=f"audit-existing-{clinic_id}",
            clinic_id=clinic_id,
            actor="system:test",
            action=AuditAction.ESCALATE,
            entity_ref=f"escalation-{clinic_id}",
            payload_hash="hash-only-evidence",
            occurred_at=NOW - timedelta(days=40),
        )
    )
    sqlite_session.flush()
    return f"patient-{clinic_id}"


def _seed_provider_graph(sqlite_session, patient_id: str) -> dict[str, ExternalEffect]:
    sent_sms, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id="clinic-gdpr",
        outreach_job_id="job-clinic-gdpr",
        idempotency_key="gdpr-sent-sms",
        available_at=NOW,
    )
    sent_sms.state = ExternalEffectState.SUCCEEDED
    sent_sms.provider_resource_id = MESSAGE_SID
    sent_sms.completed_at = NOW

    pending_sms, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id="clinic-gdpr",
        outreach_job_id="job-clinic-gdpr",
        idempotency_key="gdpr-pending-sms",
        available_at=NOW,
    )
    leased_sms, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id="clinic-gdpr",
        outreach_job_id="job-clinic-gdpr",
        idempotency_key="gdpr-leased-sms",
        available_at=NOW,
    )
    leased_sms.state = ExternalEffectState.LEASED
    leased_sms.lease_owner = "worker-before-freeze"
    leased_sms.lease_expires_at = NOW + timedelta(minutes=5)

    ambiguous_sms, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id="clinic-gdpr",
        outreach_job_id="job-clinic-gdpr",
        idempotency_key="gdpr-dispatching-sms",
        available_at=NOW,
    )
    ambiguous_sms.state = ExternalEffectState.DISPATCHING
    ambiguous_sms.dispatch_started_at = NOW
    ambiguous_sms.lease_owner = "worker-before-freeze"
    ambiguous_sms.lease_expires_at = NOW + timedelta(minutes=5)
    ambiguous_sms.attempt_count = 1

    sent_call, _ = enqueue_call_effect(
        sqlite_session,
        clinic_id="clinic-gdpr",
        outreach_job_id="job-clinic-gdpr",
        idempotency_key="gdpr-sent-call",
        available_at=NOW,
    )
    sent_call.state = ExternalEffectState.SUCCEEDED
    sent_call.provider_resource_id = CALL_SID
    sent_call.completed_at = NOW
    sqlite_session.add(
        CallRecord(
            id="call-record-gdpr",
            clinic_id="clinic-gdpr",
            patient_id=patient_id,
            external_effect_id=sent_call.id,
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=CALL_SID,
            direction=InteractionDirection.OUTBOUND,
            recording_status=CallRecordingStatus.COMPLETED,
            recording_sid=RECORDING_SID,
            recording_blob_path=BLOB_PATH,
            transcript=[{"speaker": "patient", "text": "privacy canary"}],
        )
    )
    sqlite_session.flush()
    recording_start, _ = enqueue_recording_start_effect(
        sqlite_session,
        clinic_id="clinic-gdpr",
        call_record_id="call-record-gdpr",
        available_at=NOW,
    )
    sqlite_session.flush()
    return {
        "pending_sms": pending_sms,
        "leased_sms": leased_sms,
        "ambiguous_sms": ambiguous_sms,
        "recording_start": recording_start,
    }


def test_erasure_request_atomically_freezes_and_inventories_without_locator_copy(
    sqlite_session,
):
    patient_id = _seed_patient_graph(sqlite_session)
    seeded_effects = _seed_provider_graph(sqlite_session, patient_id)

    first = request_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        patient_id=patient_id,
        confirm_token=erasure_confirm_token(patient_id),
        request_identity="tests-only-request-one",
        actor_role="dpo",
        actor_reference="tests-only-operator",
        keyring=TEST_KEYRING,
        policy=TEST_POLICY,
        now=NOW,
    )
    second = request_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        patient_id=patient_id,
        confirm_token=erasure_confirm_token(patient_id),
        request_identity="tests-only-request-two",
        actor_role="dpo",
        actor_reference="tests-only-other-operator",
        keyring=TEST_KEYRING,
        policy=TEST_POLICY,
        now=NOW,
    )

    assert first.request_id == second.request_id
    assert first.created is True
    assert second.created is False
    request = sqlite_session.get(RightsRequest, first.request_id)
    assert request is not None
    assert request.state.value == "frozen"
    assert request.subject_key_hash not in {"P-clinic-gdpr", patient_id}
    assert len(request.subject_key_hash) == 64

    targets = sqlite_session.execute(
        select(RightsTarget).where(RightsTarget.request_id == first.request_id)
    ).scalars().all()
    assert len(targets) == len({target.target_key_hash for target in targets})
    assert sum(
        target.system.value == "twilio" and target.resource.value == "call"
        for target in targets
    ) == 1
    assert {
        (target.system.value, target.resource.value, target.action.value)
        for target in targets
    }.issuperset(
        {
            ("twilio", "message", "delete"),
            ("twilio", "call", "delete"),
            ("twilio", "recording", "delete"),
            ("twilio", "transcription_collection", "purge"),
            ("azure_blob", "blob_collection", "purge"),
        }
    )
    forbidden = {MESSAGE_SID, CALL_SID, RECORDING_SID, BLOB_PATH, patient_id, "P-clinic-gdpr"}
    assert all(
        value not in repr((target.__dict__,))
        for target in targets
        for value in forbidden
    )

    rights_effects = sqlite_session.execute(
        select(ExternalEffect).where(ExternalEffect.effect_type == ExternalEffectType.RIGHTS)
    ).scalars().all()
    assert rights_effects
    assert all(set(effect.payload) == {"intent", "target_id", "attempt_ordinal"} for effect in rights_effects)
    assert all(
        value not in repr((effect.payload, effect.idempotency_key, effect.request_hash))
        for effect in rights_effects
        for value in forbidden
    )

    assert seeded_effects["pending_sms"].state == ExternalEffectState.CANCELED
    assert seeded_effects["leased_sms"].state == ExternalEffectState.CANCELED
    assert seeded_effects["recording_start"].state == ExternalEffectState.CANCELED
    assert seeded_effects["ambiguous_sms"].state == ExternalEffectState.DISPATCHING
    assert sqlite_session.get(CallRecord, "call-record-gdpr").recording_sid == RECORDING_SID

    with pytest.raises(SubjectFrozenError, match="subject_frozen"):
        enqueue_sms_effect(
            sqlite_session,
            clinic_id="clinic-gdpr",
            outreach_job_id="job-clinic-gdpr",
            idempotency_key="gdpr-post-freeze-sms",
            available_at=NOW,
        )
    with pytest.raises(SubjectFrozenError, match="subject_frozen"):
        enqueue_call_effect(
            sqlite_session,
            clinic_id="clinic-gdpr",
            outreach_job_id="job-clinic-gdpr",
            idempotency_key="gdpr-post-freeze-call",
            available_at=NOW,
        )
    with pytest.raises(SubjectFrozenError, match="subject_frozen"):
        enqueue_recording_start_effect(
            sqlite_session,
            clinic_id="clinic-gdpr",
            call_record_id="call-record-gdpr",
            available_at=NOW,
        )


def test_erasure_request_rolls_back_freeze_targets_and_effects(sqlite_session):
    patient_id = _seed_patient_graph(sqlite_session)
    seeded_effects = _seed_provider_graph(sqlite_session, patient_id)
    sqlite_session.commit()

    request_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        patient_id=patient_id,
        confirm_token=erasure_confirm_token(patient_id),
        request_identity="tests-only-request-rollback",
        actor_role="dpo",
        actor_reference="tests-only-operator",
        keyring=TEST_KEYRING,
        policy=TEST_POLICY,
        now=NOW,
    )
    sqlite_session.rollback()

    assert sqlite_session.execute(select(func.count()).select_from(RightsRequest)).scalar() == 0
    assert sqlite_session.execute(select(func.count()).select_from(RightsTarget)).scalar() == 0
    assert sqlite_session.execute(
        select(func.count())
        .select_from(ExternalEffect)
        .where(ExternalEffect.effect_type == ExternalEffectType.RIGHTS)
    ).scalar() == 0
    assert sqlite_session.get(Patient, patient_id) is not None
    assert sqlite_session.get(ExternalEffect, seeded_effects["pending_sms"].id).state == ExternalEffectState.PENDING
    assert sqlite_session.get(ExternalEffect, seeded_effects["leased_sms"].id).state == ExternalEffectState.LEASED


def test_erasure_request_inventories_staff_linked_inbound_provider_owners(sqlite_session):
    patient_id = _seed_patient_graph(sqlite_session)
    sqlite_session.add(
        InboundCall(
            id="inbound-call-gdpr",
            clinic_id="clinic-gdpr",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=INBOUND_CALL_SID,
            called_number="+441111111111",
            caller_number_hash="b" * 64,
            status=InboundCallStatus.COMPLETED,
        )
    )
    sqlite_session.add(
        InboundMessage(
            id="inbound-message-gdpr",
            clinic_id="clinic-gdpr",
            provider=ClinicPhoneProvider.TWILIO,
            provider_message_id=INBOUND_MESSAGE_SID,
            to_number="+442222222222",
            from_number_hash="c" * 64,
            body_length=12,
            body_sha256="d" * 64,
            status=InboundMessageStatus.ROUTED,
        )
    )
    sqlite_session.flush()
    sqlite_session.add_all(
        [
            InboundStaffTask(
                id="inbound-task-call-gdpr",
                clinic_id="clinic-gdpr",
                inbound_call_id="inbound-call-gdpr",
                patient_id=patient_id,
                kind=InboundStaffTaskKind.CALLBACK,
            ),
            InboundStaffTask(
                id="inbound-task-message-gdpr",
                clinic_id="clinic-gdpr",
                inbound_message_id="inbound-message-gdpr",
                patient_id=patient_id,
                kind=InboundStaffTaskKind.BOOKING_REQUEST,
            ),
        ]
    )
    sqlite_session.flush()

    result = request_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        patient_id=patient_id,
        confirm_token=erasure_confirm_token(patient_id),
        request_identity="tests-only-inbound-request",
        actor_role="dpo",
        actor_reference="tests-only-inbound-operator",
        keyring=TEST_KEYRING,
        policy=TEST_POLICY,
        now=NOW,
    )

    targets = sqlite_session.execute(
        select(RightsTarget).where(RightsTarget.request_id == result.request_id)
    ).scalars().all()
    inbound_targets = {
        (target.owner_type.value, target.owner_id, target.resource.value)
        for target in targets
        if target.owner_type.value in {"inbound_call", "inbound_message"}
    }
    assert inbound_targets == {
        ("inbound_call", "inbound-call-gdpr", "call"),
        ("inbound_message", "inbound-message-gdpr", "message"),
    }
    assert all(
        locator not in repr(target.__dict__)
        for target in targets
        for locator in {INBOUND_CALL_SID, INBOUND_MESSAGE_SID}
    )


def test_active_recording_stop_survives_refresh_before_delete_effects_release(
    sqlite_session,
):
    patient_id = _seed_patient_graph(sqlite_session)
    seeded_effects = _seed_provider_graph(sqlite_session, patient_id)
    record = sqlite_session.get(CallRecord, "call-record-gdpr")
    assert record is not None
    record.recording_status = CallRecordingStatus.IN_PROGRESS
    record.recording_started_at = NOW - timedelta(minutes=1)
    sqlite_session.flush()

    result = request_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        patient_id=patient_id,
        confirm_token=erasure_confirm_token(patient_id),
        request_identity="tests-only-active-recording",
        actor_role="dpo",
        actor_reference="tests-only-active-recording-operator",
        keyring=TEST_KEYRING,
        policy=TEST_POLICY,
        now=NOW,
    )

    stop_effect = sqlite_session.execute(
        select(ExternalEffect).where(
            ExternalEffect.effect_type == ExternalEffectType.RECORDING,
            ExternalEffect.payload["intent"].as_string() == "recording_stop",
        )
    ).scalar_one()
    protected_targets = sqlite_session.execute(
        select(RightsTarget).where(
            RightsTarget.request_id == result.request_id,
            RightsTarget.owner_type == RightsTargetOwnerType.CALL_RECORD,
            RightsTarget.resource.in_(
                {
                    RightsTargetResource.RECORDING,
                    RightsTargetResource.TRANSCRIPTION_COLLECTION,
                    RightsTargetResource.BLOB_COLLECTION,
                }
            ),
        )
    ).scalars().all()
    assert stop_effect.state == ExternalEffectState.PENDING
    assert record.recording_status == CallRecordingStatus.STOP_PENDING
    assert protected_targets
    assert all(target.current_effect_id is None for target in protected_targets)
    assert all(target.attempt_ordinal == 0 for target in protected_targets)

    with pytest.raises(RightsCompletionBlocked, match="inventory_not_finalized"):
        complete_patient_erasure(
            sqlite_session,
            clinic_id="clinic-gdpr",
            request_id=result.request_id,
            keyring=TEST_KEYRING,
            now=NOW + timedelta(seconds=1),
            actor_role="dpo",
        )
    assert stop_effect.state == ExternalEffectState.PENDING
    assert all(target.current_effect_id is None for target in protected_targets)

    stop_effect.state = ExternalEffectState.SUCCEEDED
    stop_effect.provider_status = "recording_completed"
    stop_effect.completed_at = NOW + timedelta(seconds=2)
    record.recording_status = CallRecordingStatus.COMPLETED
    record.recording_stopped_at = NOW + timedelta(seconds=2)
    seeded_effects["ambiguous_sms"].state = ExternalEffectState.REJECTED
    seeded_effects["ambiguous_sms"].completed_at = NOW + timedelta(seconds=2)
    with pytest.raises(RightsCompletionBlocked, match="pending_target"):
        complete_patient_erasure(
            sqlite_session,
            clinic_id="clinic-gdpr",
            request_id=result.request_id,
            keyring=TEST_KEYRING,
            now=NOW + timedelta(seconds=3),
            actor_role="dpo",
        )
    assert all(target.current_effect_id is not None for target in protected_targets)
    assert all(target.attempt_ordinal == 1 for target in protected_targets)


def test_erasure_completion_requires_approved_residuals_and_minimizes_graph(
    sqlite_session,
):
    patient_id = _seed_patient_graph(sqlite_session)
    result = request_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        patient_id=patient_id,
        confirm_token=erasure_confirm_token(patient_id),
        request_identity="tests-only-completion-request",
        actor_role="dpo",
        actor_reference="tests-only-completion-operator",
        keyring=TEST_KEYRING,
        policy=TEST_POLICY,
        now=NOW,
    )

    with pytest.raises(RightsCompletionBlocked, match="unapproved_residual"):
        complete_patient_erasure(
            sqlite_session,
            clinic_id="clinic-gdpr",
            request_id=result.request_id,
            keyring=TEST_KEYRING,
            now=NOW,
            actor_role="dpo",
        )

    residual_targets = sqlite_session.execute(
        select(RightsTarget).where(
            RightsTarget.request_id == result.request_id,
            RightsTarget.state == "residual",
        )
    ).scalars().all()
    approvals = {
        target.residual_category: ResidualApproval(
            category=target.residual_category,
            policy_version="tests-only-residual-policy-v1",
            approval_evidence_hash="b" * 64,
            due_at=NOW + timedelta(days=90),
            completion_eligible=True,
        )
        for target in residual_targets
        if target.residual_category is not None
    }
    applied = apply_residual_approvals(
        sqlite_session,
        clinic_id="clinic-gdpr",
        request_id=result.request_id,
        approvals=approvals,
        now=NOW,
        actor_role="dpo",
    )
    repeated_approval = apply_residual_approvals(
        sqlite_session,
        clinic_id="clinic-gdpr",
        request_id=result.request_id,
        approvals=approvals,
        now=NOW,
        actor_role="dpo",
    )
    completed = complete_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        request_id=result.request_id,
        keyring=TEST_KEYRING,
        now=NOW,
        actor_role="dpo",
    )

    assert applied == len(residual_targets)
    assert repeated_approval == 0
    assert completed.state.value == "completed"
    assert sqlite_session.get(Patient, patient_id) is None
    assert sqlite_session.execute(select(func.count()).select_from(Appointment)).scalar() == 0
    assert sqlite_session.execute(select(func.count()).select_from(OutreachJob)).scalar() == 0
    assert sqlite_session.execute(select(func.count()).select_from(Interaction)).scalar() == 0
    assert sqlite_session.execute(select(func.count()).select_from(BookingAction)).scalar() == 0
    assert sqlite_session.execute(select(func.count()).select_from(Escalation)).scalar() == 0
    request = sqlite_session.get(RightsRequest, result.request_id)
    assert request is not None
    assert request.patient_id is None
    assert request.completion_evidence_hash
    local_target = sqlite_session.execute(
        select(RightsTarget).where(
            RightsTarget.request_id == result.request_id,
            RightsTarget.resource == "patient_graph",
        )
    ).scalar_one()
    assert local_target.state.value == "verified"
    assert local_target.locator_cleared_at == NOW

    duplicate = request_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        patient_id=patient_id,
        confirm_token=erasure_confirm_token(patient_id),
        request_identity="tests-only-completion-duplicate",
        actor_role="dpo",
        actor_reference="tests-only-other-operator",
        keyring=TEST_KEYRING,
        policy=TEST_POLICY,
        now=NOW + timedelta(minutes=1),
    )
    status = get_rights_request_status(
        sqlite_session,
        clinic_id="clinic-gdpr",
        request_id=result.request_id,
        now=NOW + timedelta(minutes=1),
    )
    assert duplicate.request_id == result.request_id
    assert duplicate.created is False
    assert status.state.value == "completed"
    assert status.pending_count == 0
    assert status.unapproved_residual_count == 0
    assert status.overdue_count == 0


def test_residual_approval_category_must_match_its_mapping_key(sqlite_session):
    patient_id = _seed_patient_graph(sqlite_session)
    result = request_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        patient_id=patient_id,
        confirm_token=erasure_confirm_token(patient_id),
        request_identity="tests-only-category-request",
        actor_role="dpo",
        actor_reference="tests-only-category-operator",
        keyring=TEST_KEYRING,
        policy=TEST_POLICY,
        now=NOW,
    )
    target = sqlite_session.execute(
        select(RightsTarget).where(
            RightsTarget.request_id == result.request_id,
            RightsTarget.state == "residual",
        )
    ).scalars().first()
    assert target is not None and target.residual_category is not None
    wrong_category = next(
        category
        for category in RightsResidualCategory
        if category != target.residual_category
    )

    with pytest.raises(ValueError, match="category"):
        apply_residual_approvals(
            sqlite_session,
            clinic_id="clinic-gdpr",
            request_id=result.request_id,
            approvals={
                target.residual_category: ResidualApproval(
                    category=wrong_category,
                    policy_version="tests-only-residual-policy-v1",
                    approval_evidence_hash="b" * 64,
                    due_at=NOW + timedelta(days=90),
                    completion_eligible=True,
                )
            },
            now=NOW,
            actor_role="dpo",
        )

    assert target.residual_policy_version is None


def test_ready_erasure_finalizer_applies_policy_and_completes_once(sqlite_session):
    patient_id = _seed_patient_graph(sqlite_session)
    result = request_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        patient_id=patient_id,
        confirm_token=erasure_confirm_token(patient_id),
        request_identity="tests-only-finalizer-request",
        actor_role="dpo",
        actor_reference="tests-only-finalizer-operator",
        keyring=TEST_KEYRING,
        policy=TEST_POLICY,
        now=NOW,
    )
    residual_targets = sqlite_session.execute(
        select(RightsTarget).where(
            RightsTarget.request_id == result.request_id,
            RightsTarget.state == "residual",
        )
    ).scalars().all()
    approvals = {
        target.residual_category: ResidualApproval(
            category=target.residual_category,
            policy_version="tests-only-residual-policy-v1",
            approval_evidence_hash="c" * 64,
            due_at=NOW + timedelta(days=90),
            completion_eligible=True,
        )
        for target in residual_targets
        if target.residual_category is not None
    }

    finalized = finalize_ready_patient_erasures(
        sqlite_session,
        clinic_id="clinic-gdpr",
        keyring=TEST_KEYRING,
        approvals=approvals,
        now=NOW,
        actor_role="system",
    )
    repeated = finalize_ready_patient_erasures(
        sqlite_session,
        clinic_id="clinic-gdpr",
        keyring=TEST_KEYRING,
        approvals=approvals,
        now=NOW + timedelta(minutes=1),
        actor_role="system",
    )

    assert finalized.inspected_count == 1
    assert finalized.completed_count == 1
    assert finalized.blocked_count == 0
    assert finalized.approvals_applied == len(residual_targets)
    assert repeated.inspected_count == 0
    assert repeated.completed_count == 0
    assert sqlite_session.get(Patient, patient_id) is None


def test_completed_erasure_renews_residual_policy_without_claiming_absence(
    sqlite_session,
):
    patient_id = _seed_patient_graph(sqlite_session)
    result = request_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        patient_id=patient_id,
        confirm_token=erasure_confirm_token(patient_id),
        request_identity="tests-only-residual-maintenance",
        actor_role="dpo",
        actor_reference="tests-only-residual-operator",
        keyring=TEST_KEYRING,
        policy=TEST_POLICY,
        now=NOW,
    )
    residual_targets = sqlite_session.execute(
        select(RightsTarget).where(
            RightsTarget.request_id == result.request_id,
            RightsTarget.state == "residual",
        )
    ).scalars().all()
    initial_approvals = {
        target.residual_category: ResidualApproval(
            category=target.residual_category,
            policy_version="tests-only-residual-policy-v1",
            approval_evidence_hash="b" * 64,
            due_at=NOW + timedelta(days=90),
            completion_eligible=True,
        )
        for target in residual_targets
        if target.residual_category is not None
    }
    apply_residual_approvals(
        sqlite_session,
        clinic_id="clinic-gdpr",
        request_id=result.request_id,
        approvals=initial_approvals,
        now=NOW,
        actor_role="dpo",
    )
    complete_patient_erasure(
        sqlite_session,
        clinic_id="clinic-gdpr",
        request_id=result.request_id,
        keyring=TEST_KEYRING,
        now=NOW,
        actor_role="dpo",
    )
    request = sqlite_session.get(RightsRequest, result.request_id)
    assert request is not None
    initial_completion_hash = request.completion_evidence_hash
    renewed_approvals = {
        category: ResidualApproval(
            category=category,
            policy_version="tests-only-residual-policy-v2",
            approval_evidence_hash="c" * 64,
            due_at=NOW + timedelta(days=180),
            completion_eligible=True,
        )
        for category in initial_approvals
    }

    maintained = maintain_residual_targets(
        sqlite_session,
        clinic_id="clinic-gdpr",
        approvals=renewed_approvals,
        now=NOW + timedelta(days=91),
        actor_role="system",
        limit=50,
    )
    repeated = maintain_residual_targets(
        sqlite_session,
        clinic_id="clinic-gdpr",
        approvals=renewed_approvals,
        now=NOW + timedelta(days=91),
        actor_role="system",
        limit=50,
    )
    status = get_rights_operations_status(
        sqlite_session,
        clinic_id="clinic-gdpr",
        now=NOW + timedelta(days=91),
    )

    assert maintained.inspected_count == len(residual_targets)
    assert maintained.approvals_applied == len(residual_targets)
    assert maintained.overdue_count == 0
    assert repeated.approvals_applied == 0
    assert status.ready is True
    assert status.overdue_count == 0
    assert request.completion_evidence_hash != initial_completion_hash
    assert request.residual_target_count == len(residual_targets)
    assert request.verified_target_count == request.target_count - len(residual_targets)


def test_legacy_synchronous_erasure_is_disabled_without_mutation(sqlite_session):
    patient_id = _seed_patient_graph(sqlite_session)

    with pytest.raises(RuntimeError, match="synchronous patient erasure is disabled"):
        erase_patient(
            sqlite_session,
            "clinic-gdpr",
            patient_id,
            confirm_token=erasure_confirm_token(patient_id),
            now=NOW,
            actor="staff:test",
        )

    assert sqlite_session.get(Patient, patient_id) is not None
    assert sqlite_session.execute(select(func.count()).select_from(AuditLog)).scalar() == 1


def test_retention_scheduler_enqueues_once_at_exact_deadline_without_inline_purge(
    sqlite_session,
):
    _seed_patient_graph(sqlite_session)
    sqlite_session.add(
        Interaction(
            id="interaction-recent",
            clinic_id="clinic-gdpr",
            outreach_job_id="job-clinic-gdpr",
            channel=Channel.SMS,
            direction=InteractionDirection.INBOUND,
            content="recent content stays",
            outcome=InteractionOutcome.AUTO_HANDLED,
            occurred_at=NOW - timedelta(days=3),
        )
    )
    sqlite_session.flush()

    policy = RetentionPolicy(
        version="tests-retention-v1",
        approval_evidence_hash="b" * 64,
        approved_at=NOW - timedelta(days=2),
        effective_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=90),
        retain_for=timedelta(days=40),
        request_due_after=timedelta(days=7),
    )
    before = schedule_retention_requests(
        sqlite_session,
        clinic_id="clinic-gdpr",
        keyring=TEST_KEYRING,
        policy=policy,
        now=NOW - timedelta(microseconds=1),
        enabled=True,
    )
    exact = schedule_retention_requests(
        sqlite_session,
        clinic_id="clinic-gdpr",
        keyring=TEST_KEYRING,
        policy=policy,
        now=NOW,
        enabled=True,
    )
    repeated = schedule_retention_requests(
        sqlite_session,
        clinic_id="clinic-gdpr",
        keyring=TEST_KEYRING,
        policy=policy,
        now=NOW + timedelta(days=1),
        enabled=True,
    )

    assert before.created_count == 0
    assert exact.created_count == 1
    assert repeated.created_count == 0
    assert repeated.existing_count == 1
    assert sqlite_session.get(Interaction, "interaction-clinic-gdpr").content == (
        "patient free text with PII"
    )
    assert sqlite_session.get(Interaction, "interaction-recent").content == "recent content stays"
    request = sqlite_session.execute(
        select(RightsRequest).where(RightsRequest.kind == RightsRequestKind.RETENTION)
    ).scalar_one()
    target = sqlite_session.execute(
        select(RightsTarget).where(RightsTarget.request_id == request.id)
    ).scalar_one()
    assert request.policy_version == policy.version
    assert target.system == RightsTargetSystem.LOCAL
    assert target.resource == RightsTargetResource.INTERACTION_CONTENT
    assert target.owner_type == RightsTargetOwnerType.INTERACTION
    assert target.owner_id == "interaction-clinic-gdpr"
    assert target.current_effect_id is not None
    effect = sqlite_session.get(ExternalEffect, target.current_effect_id)
    assert effect is not None
    assert effect.payload == {
        "intent": "rights_target_execute",
        "target_id": target.id,
        "attempt_ordinal": 1,
    }


def test_retention_scheduler_fails_closed_without_current_explicit_policy(
    sqlite_session,
):
    _seed_patient_graph(sqlite_session)
    current = RetentionPolicy(
        version="tests-retention-v1",
        approval_evidence_hash="b" * 64,
        approved_at=NOW - timedelta(days=2),
        effective_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=90),
        retain_for=timedelta(days=40),
        request_due_after=timedelta(days=7),
    )
    future = RetentionPolicy(
        version="tests-retention-future-v1",
        approval_evidence_hash="c" * 64,
        approved_at=NOW + timedelta(days=1),
        effective_at=NOW + timedelta(days=2),
        expires_at=NOW + timedelta(days=90),
        retain_for=timedelta(days=40),
        request_due_after=timedelta(days=7),
    )
    stale = RetentionPolicy(
        version="tests-retention-stale-v1",
        approval_evidence_hash="d" * 64,
        approved_at=NOW - timedelta(days=90),
        effective_at=NOW - timedelta(days=89),
        expires_at=NOW,
        retain_for=timedelta(days=40),
        request_due_after=timedelta(days=7),
    )

    with pytest.raises(RuntimeError, match="disabled"):
        schedule_retention_requests(
            sqlite_session,
            clinic_id="clinic-gdpr",
            keyring=TEST_KEYRING,
            policy=current,
            now=NOW,
            enabled=False,
        )
    with pytest.raises(ValueError, match="policy is required"):
        schedule_retention_requests(
            sqlite_session,
            clinic_id="clinic-gdpr",
            keyring=TEST_KEYRING,
            policy=None,
            now=NOW,
            enabled=True,
        )
    with pytest.raises(ValueError, match="future"):
        schedule_retention_requests(
            sqlite_session,
            clinic_id="clinic-gdpr",
            keyring=TEST_KEYRING,
            policy=future,
            now=NOW,
            enabled=True,
        )
    with pytest.raises(ValueError, match="expired"):
        schedule_retention_requests(
            sqlite_session,
            clinic_id="clinic-gdpr",
            keyring=TEST_KEYRING,
            policy=stale,
            now=NOW,
            enabled=True,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule_retention_requests(
            sqlite_session,
            clinic_id="clinic-gdpr",
            keyring=TEST_KEYRING,
            policy=current,
            now=NOW.replace(tzinfo=None),
            enabled=True,
        )

    assert sqlite_session.execute(select(func.count()).select_from(RightsRequest)).scalar() == 0
    assert sqlite_session.execute(select(func.count()).select_from(RightsTarget)).scalar() == 0


def test_retention_scheduler_converges_across_hmac_key_rotation(sqlite_session):
    _seed_patient_graph(sqlite_session)
    policy = RetentionPolicy(
        version="tests-retention-v1",
        approval_evidence_hash="b" * 64,
        approved_at=NOW - timedelta(days=2),
        effective_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=90),
        retain_for=timedelta(days=40),
        request_due_after=timedelta(days=7),
    )
    first = schedule_retention_requests(
        sqlite_session,
        clinic_id="clinic-gdpr",
        keyring=TEST_KEYRING,
        policy=policy,
        now=NOW,
        enabled=True,
    )
    rotated = SubjectKeyring(
        current=SubjectKey(
            version="tests-only-v2",
            secret=b"tests-only-rotated-rights-key",
        ),
        previous=(TEST_KEYRING.current,),
    )
    repeated = schedule_retention_requests(
        sqlite_session,
        clinic_id="clinic-gdpr",
        keyring=rotated,
        policy=policy,
        now=NOW + timedelta(minutes=1),
        enabled=True,
    )

    assert first.created_count == 1
    assert repeated.created_count == 0
    assert repeated.existing_count == 1
    request = sqlite_session.execute(select(RightsRequest)).scalar_one()
    assert request.subject_key_version == TEST_KEYRING.current.version
    assert sqlite_session.execute(select(func.count()).select_from(RightsTarget)).scalar() == 1


def test_rights_operations_status_reports_zero_overdue_without_identifiers(
    sqlite_session,
):
    _seed_patient_graph(sqlite_session)
    policy = RetentionPolicy(
        version="tests-retention-v1",
        approval_evidence_hash="b" * 64,
        approved_at=NOW - timedelta(days=2),
        effective_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=90),
        retain_for=timedelta(days=40),
        request_due_after=timedelta(days=7),
    )
    schedule_retention_requests(
        sqlite_session,
        clinic_id="clinic-gdpr",
        keyring=TEST_KEYRING,
        policy=policy,
        now=NOW,
        enabled=True,
    )

    current = get_rights_operations_status(
        sqlite_session,
        clinic_id="clinic-gdpr",
        now=NOW,
    )
    overdue = get_rights_operations_status(
        sqlite_session,
        clinic_id="clinic-gdpr",
        now=NOW + timedelta(days=8),
    )

    assert current.request_count == 1
    assert current.target_count == 1
    assert current.pending_count == 1
    assert current.zero_overdue is True
    assert current.ready is False
    assert overdue.overdue_count == 1
    assert overdue.zero_overdue is False
    assert not hasattr(current, "request_ids")
    assert "interaction-clinic-gdpr" not in repr(current)