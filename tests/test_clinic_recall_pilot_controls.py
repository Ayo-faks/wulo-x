"""Deterministic pilot-programme and cumulative-cohort contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from apps.artagent.backend.api.v1.endpoints.calls import router as calls_router
from apps.artagent.backend.registries.toolstore import clinic_recall as clinic_tools
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from src.clinic_recall.candidate_queue import generate_candidate_queue
from src.clinic_recall.durable.call_worker import run_once as run_call_once
from src.clinic_recall.durable.effects import claim_effects, mark_dispatching
from src.clinic_recall.durable.enqueue import (
    enqueue_call_effect,
    enqueue_cliniko_booking_effect,
    enqueue_sms_effect,
)
from src.clinic_recall.durable.worker import run_once as run_sms_once
from src.clinic_recall.enums import (
    BookingActionStatus,
    BookingActionType,
    BookingWriteBackState,
    CallRecordingStatus,
    CampaignStatus,
    CampaignType,
    Channel,
    ClinicPhoneProvider,
    ExternalEffectState,
    ExternalEffectType,
    InteractionDirection,
    OutreachState,
    PilotProgrammeState,
    RecordingConsentState,
)
from src.clinic_recall.erasure import erasure_confirm_token
from src.clinic_recall.messaging.orchestrator import run_cadence
from src.clinic_recall.messaging.send import SendAttemptResult, send_sms
from src.clinic_recall.messaging.sender import FakeMessageSender
from src.clinic_recall.models import (
    Appointment,
    AuditLog,
    BookingAction,
    CallRecord,
    Campaign,
    Clinic,
    ExternalEffect,
    OutreachJob,
    Patient,
    PilotParticipant,
    RightsTarget,
)
from src.clinic_recall.outbox import list_outbox_items
from src.clinic_recall.pilot_controls import (
    OperationalSwitchSnapshot,
    PilotControlError,
    PilotGateDecision,
    close_programme,
    create_programme,
    enroll_participant,
    evaluate_patient_gate,
    evaluate_recording_gate,
    job_gate_for_snapshot,
    mark_programme_dark,
    operational_switch_snapshot_from_environment,
    patient_gate_for_snapshot,
    pause_programme,
    release_cumulative_limit,
)
from src.clinic_recall.recording import ensure_call_record
from src.clinic_recall.rights import (
    ResidualApproval,
    RightsPolicy,
    SubjectFrozenError,
    SubjectKey,
    SubjectKeyring,
    apply_residual_approvals,
    complete_patient_erasure,
    request_patient_erasure,
)
from src.clinic_recall.voice_worker import FakeCallInitiator

NOW = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


def _seed_clinic_and_patients(session, count: int = 51) -> str:
    clinic_id = "clinic-pilot"
    session.add(Clinic(id=clinic_id, name="Pilot Clinic"))
    session.add_all(
        Patient(
            id=f"patient-{ordinal:02d}",
            clinic_id=clinic_id,
            source_ref=f"P-{ordinal:02d}",
            name=f"Patient {ordinal}",
            phone=f"+44770091{ordinal:04d}",
            consent_flags={"sms": True, "call": True},
            opt_out_flags={},
        )
        for ordinal in range(1, count + 1)
    )
    session.flush()
    return clinic_id


def _fresh_switches(now: datetime = NOW) -> OperationalSwitchSnapshot:
    return OperationalSwitchSnapshot(
        outreach_enabled=True,
        voice_enabled=True,
        recording_enabled=False,
        refreshed_at=now - timedelta(seconds=5),
        max_age=timedelta(seconds=60),
        environment="production",
        release_identity="sha256:release-r1",
    )


def _qualify_dark(session, clinic_id: str, programme_id: str) -> None:
    mark_programme_dark(
        session,
        clinic_id=clinic_id,
        programme_id=programme_id,
        actor="operator@example.test",
        evidence_hash="d" * 64,
        now=NOW - timedelta(minutes=1),
    )


def test_first_wave_is_cumulative_and_hard_cap_is_50(sqlite_session) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session)
    programme = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-production-r1",
        environment="production",
        release_identity="sha256:release-r1",
    )
    _qualify_dark(sqlite_session, clinic_id, programme.id)
    assert programme.state == PilotProgrammeState.DARK

    participants = [
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id=f"patient-{ordinal:02d}",
            now=NOW,
        )
        for ordinal in range(1, 7)
    ]
    assert [participant.ordinal for participant in participants] == list(range(1, 7))
    assert [participant.wave for participant in participants] == [1, 1, 1, 1, 1, 2]

    with pytest.raises(PilotControlError, match="next cumulative limit"):
        release_cumulative_limit(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            cumulative_limit=15,
            actor="operator@example.test",
            evidence_hash="a" * 64,
            now=NOW,
        )

    release_cumulative_limit(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id=programme.id,
        cumulative_limit=5,
        actor="operator@example.test",
        evidence_hash="a" * 64,
        now=NOW,
    )
    assert programme.state == PilotProgrammeState.ACTIVE
    assert programme.active_cumulative_limit == 5

    rows = list(
        sqlite_session.execute(
            select(PilotParticipant).order_by(PilotParticipant.ordinal)
        ).scalars()
    )
    assert [row.ordinal for row in rows] == list(range(1, 7))
    assert all(row.released_at == NOW for row in rows[:5])
    assert rows[5].released_at is None

    for ordinal in range(1, 6):
        decision = evaluate_patient_gate(
            sqlite_session,
            clinic_id=clinic_id,
            patient_id=f"patient-{ordinal:02d}",
            channel=Channel.SMS,
            switches=_fresh_switches(),
            now=NOW,
        )
        assert decision.allowed is True
        assert decision.reason == "allowed"

    denied = evaluate_patient_gate(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id="patient-06",
        channel=Channel.SMS,
        switches=_fresh_switches(),
        now=NOW,
    )
    assert denied.allowed is False
    assert denied.reason == "participant_not_released"

    for ordinal in range(7, 51):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id=f"patient-{ordinal:02d}",
            now=NOW,
        )

    with pytest.raises(PilotControlError, match="50 unique patients"):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id="patient-51",
            now=NOW,
        )

    first = sqlite_session.scalar(
        select(PilotParticipant).where(PilotParticipant.patient_id == "patient-01")
    )
    assert first is not None
    assert first.ordinal == 1


def test_create_programme_rejects_reused_id_for_different_release(sqlite_session) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=1)
    created = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-reused-id",
        environment="production",
        release_identity="sha256:release-r1",
    )

    same = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-reused-id",
        environment=" Production ",
        release_identity=" sha256:release-r1 ",
    )
    assert same is created

    with pytest.raises(PilotControlError, match="programme id already belongs to another release"):
        create_programme(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id="pilot-reused-id",
            environment="staging",
            release_identity="sha256:release-r2",
        )


def test_pause_denies_gate_and_cancels_only_undispatched_effects(sqlite_session) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=5)
    programme = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-production-r1",
        environment="production",
        release_identity="sha256:release-r1",
    )
    _qualify_dark(sqlite_session, clinic_id, programme.id)
    for ordinal in range(1, 6):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id=f"patient-{ordinal:02d}",
            now=NOW,
        )
    release_cumulative_limit(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id=programme.id,
        cumulative_limit=5,
        actor="operator@example.test",
        evidence_hash="a" * 64,
        now=NOW,
    )
    sqlite_session.add(
        Campaign(
            id="campaign-pilot-pause",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-pilot-pause",
            clinic_id=clinic_id,
            patient_id="patient-01",
            source_ref="appointment-pilot-pause",
            status="missed",
            start_at=NOW - timedelta(days=1),
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-pilot-pause",
            clinic_id=clinic_id,
            campaign_id="campaign-pilot-pause",
            patient_id="patient-01",
            appointment_id="appointment-pilot-pause",
            channel=Channel.SMS,
            state=OutreachState.QUEUED,
        )
    )
    sqlite_session.flush()
    dispatching_action = BookingAction(
        id="booking-action-pilot-dispatching",
        clinic_id=clinic_id,
        appointment_id="appointment-pilot-pause",
        outreach_job_id="job-pilot-pause",
        type=BookingActionType.BOOK,
        status=BookingActionStatus.COMPLETED,
        request_hash="a" * 64,
        write_back_state=BookingWriteBackState.NOT_ATTEMPTED,
        written_back=False,
    )
    sqlite_session.add(dispatching_action)
    sqlite_session.flush()
    dispatching_cliniko, _ = enqueue_cliniko_booking_effect(
        sqlite_session,
        clinic_id=clinic_id,
        booking_action_id=dispatching_action.id,
        intent="create",
        available_at=NOW,
    )
    pending_sms, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-pilot-pause",
        idempotency_key="pilot-pause:sms",
        available_at=NOW,
    )
    dispatching_call, _ = enqueue_call_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-pilot-pause",
        idempotency_key="pilot-pause:call",
        available_at=NOW,
    )
    sqlite_session.commit()
    claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="pilot-pause-call-worker",
        now=NOW,
        lease_for=timedelta(minutes=5),
        limit=1,
        effect_types=(ExternalEffectType.CALL,),
    )
    mark_dispatching(
        sqlite_session,
        clinic_id=clinic_id,
        effect_id=dispatching_call.id,
        worker_id="pilot-pause-call-worker",
        now=NOW,
    )
    claim_effects(
        sqlite_session,
        clinic_id=clinic_id,
        worker_id="pilot-pause-cliniko-worker",
        now=NOW,
        lease_for=timedelta(minutes=5),
        limit=1,
        effect_types=(ExternalEffectType.CLINIKO_BOOKING,),
    )
    mark_dispatching(
        sqlite_session,
        clinic_id=clinic_id,
        effect_id=dispatching_cliniko.id,
        worker_id="pilot-pause-cliniko-worker",
        now=NOW,
    )
    dispatching_action.write_back_state = BookingWriteBackState.DISPATCHING
    dispatching_action.provider_attempted_at = NOW
    pending_action = BookingAction(
        id="booking-action-pilot-pending",
        clinic_id=clinic_id,
        appointment_id="appointment-pilot-pause",
        outreach_job_id="job-pilot-pause",
        type=BookingActionType.BOOK,
        status=BookingActionStatus.COMPLETED,
        request_hash="b" * 64,
        write_back_state=BookingWriteBackState.NOT_ATTEMPTED,
        written_back=False,
    )
    sqlite_session.add(pending_action)
    sqlite_session.flush()
    pending_cliniko, _ = enqueue_cliniko_booking_effect(
        sqlite_session,
        clinic_id=clinic_id,
        booking_action_id=pending_action.id,
        intent="create",
        available_at=NOW,
    )
    active_recording = ensure_call_record(
        sqlite_session,
        clinic_id,
        provider=ClinicPhoneProvider.TWILIO,
        provider_call_id="CA" + "9" * 32,
        session_id="pilot-pause-recording",
        direction=InteractionDirection.OUTBOUND,
        scenario="rebooking",
        patient_id="patient-01",
        consent_snapshot=None,
        now=NOW,
    )
    active_recording.consent_state = RecordingConsentState.GRANTED
    active_recording.recording_sid = "RE" + "8" * 32
    active_recording.recording_status = CallRecordingStatus.IN_PROGRESS
    active_recording.recording_started_at = NOW
    sqlite_session.commit()

    pause_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id=programme.id,
        actor="operator@example.test",
        reason="critical_invariant",
        now=NOW + timedelta(minutes=1),
    )
    late_effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-pilot-pause",
        idempotency_key="pilot-pause:late-sms",
        available_at=NOW + timedelta(minutes=1),
    )
    sqlite_session.flush()
    pause_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id=programme.id,
        actor="operator@example.test",
        reason="critical_invariant",
        now=NOW + timedelta(minutes=2),
    )
    sqlite_session.flush()

    denied = evaluate_patient_gate(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id="patient-01",
        channel=Channel.SMS,
        switches=_fresh_switches(NOW + timedelta(minutes=2)),
        now=NOW + timedelta(minutes=2),
    )
    persisted_sms = sqlite_session.get(ExternalEffect, pending_sms.id)
    persisted_call = sqlite_session.get(ExternalEffect, dispatching_call.id)
    persisted_late = sqlite_session.get(ExternalEffect, late_effect.id)
    persisted_pending_cliniko = sqlite_session.get(
        ExternalEffect,
        pending_cliniko.id,
    )
    persisted_dispatching_cliniko = sqlite_session.get(
        ExternalEffect,
        dispatching_cliniko.id,
    )
    persisted_active_recording = sqlite_session.get(CallRecord, active_recording.id)
    recording_stop = sqlite_session.execute(
        select(ExternalEffect).where(
            ExternalEffect.effect_type == ExternalEffectType.RECORDING,
            ExternalEffect.aggregate_type == "call_record",
        )
    ).scalar_one()
    assert programme.state == PilotProgrammeState.PAUSED
    assert programme.pause_reason == "critical_invariant"
    assert denied.allowed is False
    assert denied.reason == "programme_not_active"
    assert persisted_pending_cliniko is not None
    assert persisted_pending_cliniko.state == ExternalEffectState.CANCELED
    assert pending_action.write_back_state == BookingWriteBackState.REJECTED
    assert persisted_dispatching_cliniko is not None
    assert persisted_dispatching_cliniko.state == ExternalEffectState.RECONCILE_REQUIRED
    assert dispatching_action.write_back_state == BookingWriteBackState.RECONCILE_REQUIRED
    assert persisted_sms is not None
    assert persisted_sms.state == ExternalEffectState.CANCELED
    assert persisted_sms.last_error_code == "pilot_programme_paused"
    assert persisted_call is not None
    assert persisted_call.state == ExternalEffectState.DISPATCHING
    assert persisted_late is not None
    assert persisted_late.state == ExternalEffectState.CANCELED
    assert persisted_active_recording is not None
    assert persisted_active_recording.recording_status == CallRecordingStatus.STOP_PENDING
    assert recording_stop.payload == {
        "intent": "recording_stop",
        "call_record_id": active_recording.id,
    }


def test_cumulative_wave_additions_are_exact_and_prior_release_is_immutable(
    sqlite_session,
) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=50)
    programme = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-production-r1",
        environment="production",
        release_identity="sha256:release-r1",
    )
    _qualify_dark(sqlite_session, clinic_id, programme.id)
    for ordinal in range(1, 51):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id=f"patient-{ordinal:02d}",
            now=NOW,
        )

    release_times = [NOW + timedelta(days=index) for index in range(4)]
    expected_additions = [5, 10, 15, 20]
    prior_released = 0
    prior_released_ids: set[str] = set()
    for cumulative_limit, release_at, expected_added in zip(
        (5, 15, 30, 50),
        release_times,
        expected_additions,
        strict=True,
    ):
        release_cumulative_limit(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            cumulative_limit=cumulative_limit,
            actor="operator@example.test",
            evidence_hash=f"{cumulative_limit // 5:x}" * 64,
            now=release_at,
        )
        rows = list(
            sqlite_session.scalars(select(PilotParticipant).order_by(PilotParticipant.ordinal))
        )
        released_ids = {
            participant.id for participant in rows if participant.released_at is not None
        }
        assert len(released_ids - prior_released_ids) == expected_added
        assert len(released_ids) == prior_released + expected_added
        first_released_at = rows[0].released_at
        assert first_released_at is not None
        if first_released_at.tzinfo is None:
            first_released_at = first_released_at.replace(tzinfo=UTC)
        assert first_released_at == release_times[0]
        prior_released_ids = released_ids
        prior_released = cumulative_limit

    with pytest.raises(PilotControlError, match="already at terminal cumulative limit"):
        release_cumulative_limit(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            cumulative_limit=50,
            actor="operator@example.test",
            evidence_hash="f" * 64,
            now=NOW + timedelta(days=4),
        )

    release_audits = list(
        sqlite_session.scalars(
            select(AuditLog).where(
                AuditLog.action == "approve",
                AuditLog.entity_ref.like(f"{programme.id}:wave:%"),
            )
        )
    )
    assert {audit.entity_ref for audit in release_audits} == {
        f"{programme.id}:wave:5",
        f"{programme.id}:wave:15",
        f"{programme.id}:wave:30",
        f"{programme.id}:wave:50",
    }
    assert all(audit.actor == "operator@example.test" for audit in release_audits)

    close_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id=programme.id,
        actor="operator@example.test",
        reason="pilot_complete",
        now=NOW + timedelta(days=5),
    )
    closed = evaluate_patient_gate(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id="patient-01",
        channel=Channel.SMS,
        switches=_fresh_switches(NOW + timedelta(days=5)),
        now=NOW + timedelta(days=5),
    )
    assert programme.state == PilotProgrammeState.CLOSED
    assert closed.allowed is False
    assert closed.reason == "programme_not_active"


def test_operational_switch_environment_is_explicit_fresh_and_recording_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "CLINIC_RECALL_PILOT_OUTREACH_ENABLED",
        "CLINIC_RECALL_PILOT_VOICE_ENABLED",
        "CLINIC_RECALL_PILOT_RECORDING_ENABLED",
        "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT",
        "CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS",
        "CLINIC_RECALL_PILOT_ENVIRONMENT",
        "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    missing = operational_switch_snapshot_from_environment()
    assert missing.decision(Channel.SMS, NOW).allowed is False
    assert missing.decision(Channel.SMS, NOW).reason == "configuration_identity_missing"
    assert missing.recording_decision(NOW).allowed is False

    monkeypatch.setenv("CLINIC_RECALL_PILOT_OUTREACH_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_VOICE_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_RECORDING_ENABLED", "false")
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT",
        (NOW - timedelta(seconds=5)).isoformat(),
    )
    monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS", "60")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_ENVIRONMENT", " Production ")
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
        " sha256:release-r1 ",
    )
    snapshot = operational_switch_snapshot_from_environment()
    assert snapshot.environment == "production"
    assert snapshot.release_identity == "sha256:release-r1"
    assert snapshot.decision(Channel.SMS, NOW).allowed is True
    assert snapshot.decision(Channel.CALL, NOW).allowed is True
    assert snapshot.recording_decision(NOW).allowed is False
    assert snapshot.recording_decision(NOW).reason == "recording_switch_disabled"

    monkeypatch.setenv("CLINIC_RECALL_PILOT_RECORDING_ENABLED", "true")
    assert operational_switch_snapshot_from_environment().recording_decision(NOW).allowed is True
    monkeypatch.setenv("CLINIC_RECALL_PILOT_VOICE_ENABLED", "unexpected")
    assert (
        operational_switch_snapshot_from_environment().decision(Channel.CALL, NOW).allowed is False
    )
    monkeypatch.setenv("CLINIC_RECALL_PILOT_VOICE_ENABLED", "true")
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT",
        (NOW + timedelta(seconds=1)).isoformat(),
    )
    assert (
        operational_switch_snapshot_from_environment().decision(Channel.SMS, NOW).reason
        == "configuration_stale"
    )


def test_recording_gate_requires_active_matching_database_programme(sqlite_session) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=5)
    programme = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-production-r1",
        environment="production",
        release_identity="sha256:release-r1",
    )
    switches = OperationalSwitchSnapshot(
        outreach_enabled=True,
        voice_enabled=True,
        recording_enabled=True,
        refreshed_at=NOW - timedelta(seconds=5),
        max_age=timedelta(seconds=60),
        environment="production",
        release_identity="sha256:release-r1",
    )
    draft = evaluate_recording_gate(
        sqlite_session,
        clinic_id=clinic_id,
        switches=switches,
        now=NOW,
    )
    _qualify_dark(sqlite_session, clinic_id, programme.id)
    for ordinal in range(1, 6):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id=f"patient-{ordinal:02d}",
            now=NOW,
        )
    release_cumulative_limit(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id=programme.id,
        cumulative_limit=5,
        actor="operator@example.test",
        evidence_hash="a" * 64,
        now=NOW,
    )
    active = evaluate_recording_gate(
        sqlite_session,
        clinic_id=clinic_id,
        switches=switches,
        now=NOW,
    )
    programme.state = PilotProgrammeState.PAUSED
    paused = evaluate_recording_gate(
        sqlite_session,
        clinic_id=clinic_id,
        switches=switches,
        now=NOW,
    )

    assert draft.allowed is False
    assert draft.reason == "programme_not_active"
    assert active.allowed is True
    assert paused.allowed is False
    assert paused.reason == "programme_not_active"


def test_patient_erasure_preserves_counted_participant_without_direct_identity(
    sqlite_session,
) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=5)
    programme = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-production-r1",
        environment="production",
        release_identity="sha256:release-r1",
    )
    _qualify_dark(sqlite_session, clinic_id, programme.id)
    participants = [
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id=f"patient-{ordinal:02d}",
            now=NOW,
        )
        for ordinal in range(1, 6)
    ]
    participant = participants[0]
    release_cumulative_limit(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id=programme.id,
        cumulative_limit=5,
        actor="operator@example.test",
        evidence_hash="a" * 64,
        now=NOW,
    )
    participant_id = participant.id
    participant_hash = participant.patient_key_hash

    keyring = SubjectKeyring(
        current=SubjectKey("tests-pilot-erasure-v1", b"tests-pilot-erasure-key")
    )
    result = request_patient_erasure(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id="patient-01",
        confirm_token=erasure_confirm_token("patient-01"),
        request_identity="tests-pilot-erasure-request",
        actor_role="dpo",
        actor_reference="tests-pilot-erasure-operator",
        keyring=keyring,
        policy=RightsPolicy(
            "tests-pilot-erasure-policy-v1",
            "a" * 64,
            timedelta(days=28),
        ),
        now=NOW,
    )
    residual_targets = (
        sqlite_session.execute(
            select(RightsTarget).where(
                RightsTarget.request_id == result.request_id,
                RightsTarget.state == "residual",
            )
        )
        .scalars()
        .all()
    )
    apply_residual_approvals(
        sqlite_session,
        clinic_id=clinic_id,
        request_id=result.request_id,
        approvals={
            target.residual_category: ResidualApproval(
                category=target.residual_category,
                policy_version="tests-pilot-residual-v1",
                approval_evidence_hash="b" * 64,
                due_at=NOW + timedelta(days=90),
                completion_eligible=True,
            )
            for target in residual_targets
            if target.residual_category is not None
        },
        now=NOW,
        actor_role="dpo",
    )
    completed = complete_patient_erasure(
        sqlite_session,
        clinic_id=clinic_id,
        request_id=result.request_id,
        keyring=keyring,
        now=NOW,
        actor_role="dpo",
    )

    persisted = sqlite_session.get(PilotParticipant, participant_id)
    assert completed.state.value == "completed"
    assert sqlite_session.get(Patient, "patient-01") is None
    assert persisted is not None
    assert persisted.patient_id is None
    assert persisted.patient_key_hash == participant_hash
    assert persisted.ordinal == 1
    sqlite_session.add(
        Patient(
            id="patient-01",
            clinic_id=clinic_id,
            source_ref="P-01-recreated",
            name="Recreated Patient",
            phone="+447700919999",
            consent_flags={"sms": True},
            opt_out_flags={},
        )
    )
    sqlite_session.flush()
    denied = evaluate_patient_gate(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id="patient-01",
        channel=Channel.SMS,
        switches=_fresh_switches(),
        now=NOW,
    )
    assert denied.allowed is False
    assert denied.reason == "participant_identity_erased"


def test_frozen_subject_cannot_be_enrolled_in_pilot(sqlite_session) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=1)
    programme = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-freeze-r1",
        environment="production",
        release_identity="sha256:freeze-r1",
    )
    sqlite_session.add(
        Campaign(
            id="campaign-freeze-cliniko",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        Appointment(
            id="appointment-freeze-cliniko",
            clinic_id=clinic_id,
            patient_id="patient-01",
            source_ref="appointment-freeze-cliniko",
            status="missed",
            start_at=NOW - timedelta(days=1),
        )
    )
    sqlite_session.flush()
    sqlite_session.add(
        OutreachJob(
            id="job-freeze-cliniko",
            clinic_id=clinic_id,
            campaign_id="campaign-freeze-cliniko",
            patient_id="patient-01",
            appointment_id="appointment-freeze-cliniko",
            channel=Channel.CALL,
            state=OutreachState.COMPLETED,
        )
    )
    sqlite_session.flush()
    action = BookingAction(
        id="booking-action-freeze-cliniko",
        clinic_id=clinic_id,
        appointment_id="appointment-freeze-cliniko",
        outreach_job_id="job-freeze-cliniko",
        type=BookingActionType.BOOK,
        status=BookingActionStatus.COMPLETED,
        request_hash="c" * 64,
        write_back_state=BookingWriteBackState.NOT_ATTEMPTED,
        written_back=False,
    )
    sqlite_session.add(action)
    sqlite_session.flush()
    effect, _ = enqueue_cliniko_booking_effect(
        sqlite_session,
        clinic_id=clinic_id,
        booking_action_id=action.id,
        intent="create",
        available_at=NOW,
    )
    request_patient_erasure(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id="patient-01",
        confirm_token="ERASE patient-01",
        request_identity="tests-pilot-freeze",
        actor_role="dpo",
        actor_reference="tests-pilot-freeze-operator",
        keyring=SubjectKeyring(
            current=SubjectKey("tests-pilot-freeze-v1", b"tests-pilot-freeze-key")
        ),
        policy=RightsPolicy("tests-pilot-freeze-policy-v1", "a" * 64, timedelta(days=28)),
        now=NOW,
    )

    assert effect.state == ExternalEffectState.CANCELED
    assert action.write_back_state == BookingWriteBackState.REJECTED

    with pytest.raises(SubjectFrozenError, match="subject_frozen"):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id="patient-01",
            now=NOW,
        )

    assert sqlite_session.execute(select(PilotParticipant)).scalars().all() == []


def test_candidate_generation_queues_only_released_participants(sqlite_session) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=6)
    programme = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-production-r1",
        environment="production",
        release_identity="sha256:release-r1",
    )
    _qualify_dark(sqlite_session, clinic_id, programme.id)
    for ordinal in range(1, 7):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id=f"patient-{ordinal:02d}",
            now=NOW,
        )
        sqlite_session.add(
            Appointment(
                id=f"appointment-{ordinal:02d}",
                clinic_id=clinic_id,
                patient_id=f"patient-{ordinal:02d}",
                source_ref=f"A-{ordinal:02d}",
                status="missed",
                start_at=NOW - timedelta(days=1),
            )
        )
    release_cumulative_limit(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id=programme.id,
        cumulative_limit=5,
        actor="operator@example.test",
        evidence_hash="a" * 64,
        now=NOW,
    )
    sqlite_session.flush()

    result = generate_candidate_queue(
        sqlite_session,
        clinic_id,
        NOW,
        pilot_gate=patient_gate_for_snapshot(_fresh_switches()),
    )

    jobs = list(sqlite_session.scalars(select(OutreachJob)))
    assert result.queued == 5
    assert result.skipped["participant_not_released"] == 1
    assert {job.patient_id for job in jobs} == {
        "patient-01",
        "patient-02",
        "patient-03",
        "patient-04",
        "patient-05",
    }

    campaign = sqlite_session.get(Campaign, f"campaign-detection-{clinic_id}")
    assert campaign is not None
    campaign.status = CampaignStatus.ACTIVE
    sqlite_session.add(
        OutreachJob(
            id="job-patient-06-manual",
            clinic_id=clinic_id,
            campaign_id=campaign.id,
            patient_id="patient-06",
            appointment_id="appointment-06",
            channel=Channel.SMS,
            state=OutreachState.QUEUED,
        )
    )
    sqlite_session.flush()

    cadence = run_cadence(
        sqlite_session,
        clinic_id,
        NOW,
        pilot_gate=patient_gate_for_snapshot(_fresh_switches()),
    )
    out_of_wave_effect = sqlite_session.scalar(
        select(ExternalEffect).where(ExternalEffect.aggregate_id == "job-patient-06-manual")
    )
    assert cadence.sms_enqueued == 5
    assert cadence.skipped["participant_not_released"] == 1
    assert out_of_wave_effect is None

    sqlite_session.commit()
    sender = FakeMessageSender()
    dispatched = run_sms_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="pilot-first-contact-worker",
        sender=sender,
        programme_gate=job_gate_for_snapshot(_fresh_switches(), Channel.SMS),
        now=NOW + timedelta(seconds=1),
        enabled=True,
        limit=5,
    )
    sqlite_session.expire_all()
    participants = list(
        sqlite_session.scalars(select(PilotParticipant).order_by(PilotParticipant.ordinal))
    )
    assert dispatched.succeeded == 5
    assert len(sender.sms_messages) == 5
    assert all(participant.first_contact_at is not None for participant in participants[:5])
    assert participants[5].first_contact_at is None
    outbox = list_outbox_items(
        sqlite_session,
        clinic_id,
        NOW + timedelta(seconds=2),
        pilot_gate=patient_gate_for_snapshot(_fresh_switches()),
    )
    assert len(outbox) == 1
    assert outbox[0].outreach_job_id == "job-patient-06-manual"
    assert outbox[0].eligible_now is False
    assert outbox[0].skip_reason == "participant_not_released"
    assert outbox[0].can_send_after_approval is False


def test_sms_worker_rechecks_database_programme_after_effect_enqueue(
    sqlite_session,
) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=5)
    programme = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-production-r1",
        environment="production",
        release_identity="sha256:release-r1",
    )
    _qualify_dark(sqlite_session, clinic_id, programme.id)
    for ordinal in range(1, 6):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id=f"patient-{ordinal:02d}",
            now=NOW,
        )
    release_cumulative_limit(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id=programme.id,
        cumulative_limit=5,
        actor="operator@example.test",
        evidence_hash="a" * 64,
        now=NOW,
    )
    sqlite_session.add(
        Campaign(
            id="campaign-sms-last-mile",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-sms-last-mile",
            clinic_id=clinic_id,
            campaign_id="campaign-sms-last-mile",
            patient_id="patient-01",
            channel=Channel.SMS,
            state=OutreachState.QUEUED,
        )
    )
    sqlite_session.flush()
    effect, _ = enqueue_sms_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-sms-last-mile",
        idempotency_key="pilot:last-mile:sms",
        available_at=NOW,
    )
    sqlite_session.commit()
    programme.state = PilotProgrammeState.PAUSED
    programme.paused_at = NOW + timedelta(seconds=1)
    programme.pause_reason = "operator_pause"
    sqlite_session.commit()
    sender = FakeMessageSender()

    result = run_sms_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="pilot-sms-worker",
        sender=sender,
        programme_gate=job_gate_for_snapshot(_fresh_switches(), Channel.SMS),
        now=NOW + timedelta(seconds=2),
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.claimed == 1
    assert result.canceled == 1
    assert result.succeeded == 0
    assert sender.sms_messages == []
    assert persisted is not None
    assert persisted.state == ExternalEffectState.CANCELED
    assert persisted.last_error_code == "programme_not_active"


def test_call_worker_rechecks_database_programme_after_effect_enqueue(
    sqlite_session,
) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=5)
    programme = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-production-r1",
        environment="production",
        release_identity="sha256:release-r1",
    )
    _qualify_dark(sqlite_session, clinic_id, programme.id)
    for ordinal in range(1, 6):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id=f"patient-{ordinal:02d}",
            now=NOW,
        )
    release_cumulative_limit(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id=programme.id,
        cumulative_limit=5,
        actor="operator@example.test",
        evidence_hash="a" * 64,
        now=NOW,
    )
    sqlite_session.add(
        Campaign(
            id="campaign-call-last-mile",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-call-last-mile",
            clinic_id=clinic_id,
            campaign_id="campaign-call-last-mile",
            patient_id="patient-01",
            channel=Channel.SMS,
            state=OutreachState.NO_REPLY,
        )
    )
    sqlite_session.flush()
    effect, _ = enqueue_call_effect(
        sqlite_session,
        clinic_id=clinic_id,
        outreach_job_id="job-call-last-mile",
        idempotency_key="pilot:last-mile:call",
        available_at=NOW,
    )
    sqlite_session.commit()
    programme.state = PilotProgrammeState.PAUSED
    programme.paused_at = NOW + timedelta(seconds=1)
    programme.pause_reason = "operator_pause"
    sqlite_session.commit()
    initiator = FakeCallInitiator()

    result = run_call_once(
        sessionmaker(bind=sqlite_session.bind, expire_on_commit=False),
        clinic_id=clinic_id,
        worker_id="pilot-call-worker",
        initiator=initiator,
        programme_gate=job_gate_for_snapshot(_fresh_switches(), Channel.CALL),
        now=NOW + timedelta(seconds=2),
        enabled=True,
    )

    sqlite_session.expire_all()
    persisted = sqlite_session.get(ExternalEffect, effect.id)
    assert result.claimed == 1
    assert result.canceled == 1
    assert result.provider_accepted == 0
    assert initiator.calls == []
    assert persisted is not None
    assert persisted.state == ExternalEffectState.CANCELED
    assert persisted.last_error_code == "programme_not_active"


def test_direct_send_service_requires_current_programme_gate(sqlite_session) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=5)
    programme = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-production-r1",
        environment="production",
        release_identity="sha256:release-r1",
    )
    _qualify_dark(sqlite_session, clinic_id, programme.id)
    for ordinal in range(1, 6):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id=f"patient-{ordinal:02d}",
            now=NOW,
        )
    release_cumulative_limit(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id=programme.id,
        cumulative_limit=5,
        actor="operator@example.test",
        evidence_hash="a" * 64,
        now=NOW,
    )
    sqlite_session.add(
        Campaign(
            id="campaign-direct-send",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-direct-send",
            clinic_id=clinic_id,
            campaign_id="campaign-direct-send",
            patient_id="patient-01",
            channel=Channel.SMS,
            state=OutreachState.QUEUED,
        )
    )
    sqlite_session.flush()
    programme.state = PilotProgrammeState.PAUSED
    sqlite_session.flush()
    sender = FakeMessageSender()

    result = send_sms(
        sqlite_session,
        clinic_id,
        "job-direct-send",
        NOW + timedelta(seconds=1),
        sender,
        pilot_gate=job_gate_for_snapshot(_fresh_switches(), Channel.SMS),
    )

    assert result.sent is False
    assert result.pilot_reason == "programme_not_active"
    assert sender.sms_messages == []


def test_email_outbox_uses_outreach_operational_channel(sqlite_session) -> None:
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=1)
    patient = sqlite_session.get(Patient, "patient-01")
    assert patient is not None
    patient.email = "patient@example.test"
    patient.consent_flags = {"sms": True, "email": True, "call": True}
    sqlite_session.add(
        Campaign(
            id="campaign-email-outbox",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    sqlite_session.add(
        OutreachJob(
            id="job-email-outbox",
            clinic_id=clinic_id,
            campaign_id="campaign-email-outbox",
            patient_id=patient.id,
            channel=Channel.EMAIL,
            state=OutreachState.QUEUED,
        )
    )
    sqlite_session.flush()
    observed_channels: list[Channel] = []

    def pilot_gate(_session, _clinic_id, _patient_id, channel: Channel, _now):
        observed_channels.append(channel)
        return PilotGateDecision(channel == Channel.SMS, "allowed")

    items = list_outbox_items(
        sqlite_session,
        clinic_id,
        NOW,
        pilot_gate=pilot_gate,
    )

    assert observed_channels == [Channel.SMS]
    assert len(items) == 1
    assert items[0].channel == Channel.EMAIL.value
    assert items[0].eligible_now is True


@pytest.mark.asyncio
async def test_email_tool_binds_outreach_operational_gate(
    sqlite_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)
    switches = object()
    bound_gate = object()
    channels: list[Channel] = []

    def bind_gate(observed_switches, channel: Channel):
        assert observed_switches is switches
        channels.append(channel)
        return bound_gate

    def fake_send(*_args, pilot_gate, **_kwargs):
        assert pilot_gate is bound_gate
        return SendAttemptResult(sent=False, state=OutreachState.QUEUED)

    monkeypatch.setattr(clinic_tools, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(clinic_tools, "_sender", object)
    monkeypatch.setattr(
        clinic_tools,
        "operational_switch_snapshot_from_environment",
        lambda: switches,
    )
    monkeypatch.setattr(clinic_tools, "job_gate_for_snapshot", bind_gate)
    monkeypatch.setattr(clinic_tools, "deterministic_send_email", fake_send)

    await clinic_tools.send_email(
        {
            "_clinic_id": "clinic-test",
            "_outreach_job_id": "job-email",
            "now": NOW.isoformat(),
        }
    )

    assert channels == [Channel.SMS]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "service_name"),
    (
        ("send_sms", "deterministic_send_sms"),
        ("send_email", "deterministic_send_email"),
    ),
)
async def test_send_tools_surface_pilot_denial_reason(
    sqlite_session,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    service_name: str,
) -> None:
    factory = sessionmaker(bind=sqlite_session.bind, expire_on_commit=False)

    def fake_send(*_args, **_kwargs):
        return SendAttemptResult(
            sent=False,
            state=OutreachState.QUEUED,
            pilot_reason="configuration_stale",
        )

    monkeypatch.setattr(clinic_tools, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(clinic_tools, "_sender", object)
    monkeypatch.setattr(
        clinic_tools,
        "operational_switch_snapshot_from_environment",
        object,
    )
    monkeypatch.setattr(clinic_tools, "job_gate_for_snapshot", lambda *_args: object())
    monkeypatch.setattr(clinic_tools, service_name, fake_send)

    result = await getattr(clinic_tools, tool_name)(
        {
            "_clinic_id": "clinic-test",
            "_outreach_job_id": f"job-{tool_name}",
            "now": NOW.isoformat(),
        }
    )

    assert result["success"] is False
    assert result["pilot_reason"] == "configuration_stale"


def test_general_acs_endpoint_rejects_durable_clinic_recall_context() -> None:
    app = FastAPI()
    app.include_router(calls_router, prefix="/api/v1/calls")
    response = TestClient(app).post(
        "/api/v1/calls/initiate",
        json={
            "target_number": "+447700900001",
            "context": {
                "source": "clinic_recall_voice_worker",
                "clinic_id": "clinic-pilot",
                "patient_id": "patient-01",
                "outreach_job_id": "job-01",
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Clinic Recall calls require the durable Twilio CALL worker"
    )


def test_cohort_invariant_violation_emits_aggregate_event_and_fails_closed(
    sqlite_session, monkeypatch
) -> None:
    """PR-14: true invariant breaches emit one closed aggregate event."""
    from src.clinic_recall import pilot_controls as pilot_module

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        pilot_module,
        "emit_runtime_event",
        lambda name, attributes: events.append((name, dict(attributes))) or True,
    )
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=51)
    programme = create_programme(
        sqlite_session,
        clinic_id=clinic_id,
        programme_id="pilot-production-r1",
        environment="production",
        release_identity="sha256:release-r1",
    )
    _qualify_dark(sqlite_session, clinic_id, programme.id)
    for ordinal in range(1, 51):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id=f"patient-{ordinal:02d}",
            now=NOW,
        )
    assert events == []

    with pytest.raises(PilotControlError, match="limited to 50"):
        enroll_participant(
            sqlite_session,
            clinic_id=clinic_id,
            programme_id=programme.id,
            patient_id="patient-51",
            now=NOW,
        )
    assert events == [
        (
            "pilot.invariant.violation",
            {"reason_code": "cohort_limit_exceeded", "count": 1},
        )
    ]


def test_expected_policy_denial_does_not_emit_invariant_violation(
    sqlite_session, monkeypatch
) -> None:
    """PR-14: switch/config denials are policy outcomes, not invariant breaches."""
    from src.clinic_recall import pilot_controls as pilot_module

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        pilot_module,
        "emit_runtime_event",
        lambda name, attributes: events.append((name, dict(attributes))) or True,
    )
    clinic_id = _seed_clinic_and_patients(sqlite_session, count=1)
    disabled = OperationalSwitchSnapshot(
        outreach_enabled=False,
        voice_enabled=False,
        recording_enabled=False,
        refreshed_at=NOW - timedelta(seconds=5),
        max_age=timedelta(seconds=60),
        environment="production",
        release_identity="sha256:release-r1",
    )
    decision = evaluate_patient_gate(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id="patient-01",
        channel=Channel.SMS,
        switches=disabled,
        now=NOW,
    )
    stale = OperationalSwitchSnapshot(
        outreach_enabled=True,
        voice_enabled=True,
        recording_enabled=False,
        refreshed_at=NOW - timedelta(hours=2),
        max_age=timedelta(seconds=60),
        environment="production",
        release_identity="sha256:release-r1",
    )
    stale_decision = evaluate_patient_gate(
        sqlite_session,
        clinic_id=clinic_id,
        patient_id="patient-01",
        channel=Channel.SMS,
        switches=stale,
        now=NOW,
    )

    assert decision.allowed is False
    assert decision.reason == "outreach_switch_disabled"
    assert stale_decision.allowed is False
    assert stale_decision.reason == "configuration_stale"
    assert events == []


def test_invariant_still_fails_closed_when_telemetry_raises(monkeypatch) -> None:
    from src.clinic_recall import pilot_controls as pilot_module

    monkeypatch.setattr(
        pilot_module,
        "emit_runtime_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    error = pilot_module._invariant_violation(
        "cohort_limit_exceeded",
        "pilot programme is limited to 50 unique patients",
    )
    assert isinstance(error, PilotControlError)
    assert str(error) == "pilot programme is limited to 50 unique patients"
