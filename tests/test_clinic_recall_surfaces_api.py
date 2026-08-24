"""HTTP tests for Phase 4 Clinic Recall staff surfaces."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from apps.artagent.backend.api.v1.endpoints import clinic_recall
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.availability import AvailabilitySlotInput, upsert_availability_slots
from src.clinic_recall.booking import book_slot
from src.clinic_recall.enums import (
    AppointmentStatus,
    BookingActionStatus,
    CallRecordingStatus,
    CampaignStatus,
    CampaignType,
    Channel,
    ClinicPhoneProvider,
    ClinicPhonePurpose,
    ClinicPhoneStatus,
    EscalationReason,
    EscalationStatus,
    ExternalEffectState,
    ImportBatchState,
    InboundCallStatus,
    InboundMessageStatus,
    InboundStaffTaskKind,
    InboundStaffTaskStatus,
    InteractionDirection,
    InteractionOutcome,
    OutreachState,
    RecordingConsentSource,
    RecordingConsentState,
    RecordingDeletionState,
    SourceSystem,
)
from src.clinic_recall.erasure import erasure_confirm_token
from src.clinic_recall.escalation import escalate_to_staff
from src.clinic_recall.handoffs import (
    ensure_external_effect_handoff,
    ensure_handoff_receipt,
)
from src.clinic_recall.models import (
    Appointment,
    Base,
    BookingAction,
    CallRecord,
    Campaign,
    Clinic,
    ClinicIdentityMapping,
    ClinicPhoneNumber,
    Escalation,
    ExternalEffect,
    ExternalEffectHandoff,
    HandoffReceipt,
    ImportBatch,
    InboundCall,
    InboundMessage,
    InboundStaffTask,
    Interaction,
    OutreachJob,
    Patient,
)
from src.clinic_recall.pilot_controls import (
    create_programme,
    enroll_participant,
    mark_programme_dark,
    release_cumulative_limit,
)

from tests.identity_evidence_support import grant_synthetic_t2

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(clinic_recall.router, prefix="/api/v1/clinic-recall")
    return app


def _factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        _seed(session, "clinic-a")
        _seed(session, "clinic-b")
        session.commit()
    return factory


def _easy_auth_header(
    user_details: str = "staff@example.test",
    provider: str = "aad",
    user_id: str | None = None,
) -> dict[str, str]:
    principal = {
        "auth_typ": provider,
        "name_typ": "name",
        "role_typ": "roles",
        "userDetails": user_details,
        "claims": [
            {"typ": "preferred_username", "val": user_details},
            {"typ": "name", "val": "Clinic Staff"},
        ],
    }
    if user_id is not None:
        principal["userId"] = user_id
    encoded = base64.b64encode(json.dumps(principal).encode("utf-8")).decode("ascii")
    return {"x-ms-client-principal": encoded}


def _seed(session: Session, clinic_id: str) -> None:
    session.add(
        Clinic(
            id=clinic_id,
            name=f"{clinic_id} Clinic",
            timezone="Europe/London",
            contact_hours={"start_hour": 9, "end_hour": 17},
            daily_caps=200,
            branding={"sms_sender": f"{clinic_id} Recall"},
        )
    )
    session.add(
        Patient(
            id=f"patient-{clinic_id}",
            clinic_id=clinic_id,
            source_ref=f"P-{clinic_id}",
            name=f"{clinic_id} Patient",
            phone="+447700910030",
            email="surface@example.test",
            consent_flags={"sms": True, "email": True, "call": True},
            opt_out_flags={},
        )
    )
    session.add(
        Appointment(
            id=f"appointment-{clinic_id}",
            clinic_id=clinic_id,
            patient_id=f"patient-{clinic_id}",
            source_ref=f"A-{clinic_id}",
            status=AppointmentStatus.MISSED,
            start_at=NOW - timedelta(days=7),
            value=Decimal("80.00"),
        )
    )
    session.add(
        Campaign(
            id=f"campaign-{clinic_id}",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    session.add(
        OutreachJob(
            id=f"job-{clinic_id}",
            clinic_id=clinic_id,
            campaign_id=f"campaign-{clinic_id}",
            patient_id=f"patient-{clinic_id}",
            appointment_id=f"appointment-{clinic_id}",
            channel=Channel.CALL,
            state=OutreachState.NO_REPLY,
        )
    )
    session.add(
        ClinicPhoneNumber(
            id=f"phone-{clinic_id}",
            clinic_id=clinic_id,
            provider=ClinicPhoneProvider.TWILIO,
            phone_number=f"+1555000{clinic_id[-1]}",
            purpose=ClinicPhonePurpose.INBOUND,
            status=ClinicPhoneStatus.ACTIVE,
            config={"test_status": "green"},
        )
    )
    session.add(
        InboundCall(
            id=f"inbound-call-{clinic_id}",
            clinic_id=clinic_id,
            clinic_phone_number_id=f"phone-{clinic_id}",
            provider=ClinicPhoneProvider.TWILIO,
            provider_call_id=f"CA-{clinic_id}",
            called_number=f"+1555000{clinic_id[-1]}",
            caller_number_hash=f"sha256:{clinic_id}secret",
            status=InboundCallStatus.STARTED,
        )
    )
    session.add(
        InboundStaffTask(
            id=f"inbound-task-{clinic_id}",
            clinic_id=clinic_id,
            inbound_call_id=f"inbound-call-{clinic_id}",
            kind=InboundStaffTaskKind.CALLBACK,
            status=InboundStaffTaskStatus.OPEN,
            priority="normal",
            reason="callback",
            summary=f"{clinic_id} caller asked for reception",
        )
    )
    session.add(
        InboundMessage(
            id=f"inbound-message-{clinic_id}",
            clinic_id=clinic_id,
            clinic_phone_number_id=f"phone-{clinic_id}",
            provider=ClinicPhoneProvider.TWILIO,
            provider_message_id=f"SM-{clinic_id}",
            to_number=f"+1555000{clinic_id[-1]}",
            from_number_hash=f"sha256:{clinic_id}textsecret",
            direction=InteractionDirection.INBOUND,
            body_length=18,
            body_sha256=f"sha256:{clinic_id}messagehash",
            intent="booking_request",
            status=InboundMessageStatus.ROUTED,
            summary=f"{clinic_id} booking request from inbound SMS",
        )
    )
    session.add(
        InboundStaffTask(
            id=f"inbound-text-task-{clinic_id}",
            clinic_id=clinic_id,
            inbound_message_id=f"inbound-message-{clinic_id}",
            kind=InboundStaffTaskKind.BOOKING_REQUEST,
            status=InboundStaffTaskStatus.OPEN,
            priority="normal",
            reason="booking_request",
            summary=f"{clinic_id} text booking request",
        )
    )
    session.flush()
    future_slot_start = datetime.now(UTC) + timedelta(days=1)
    slot_id = upsert_availability_slots(
        session,
        clinic_id,
        [
            AvailabilitySlotInput(
                source_ref=f"slot-{clinic_id}",
                source_provider="cliniko",
                business_id="920000001",
                clinician_id="930000001",
                appointment_type_id="940000001",
                start_at=future_slot_start,
                end_at=future_slot_start + timedelta(minutes=30),
                fetched_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
        ],
        now=NOW,
    )[0].slot_id
    identity_service, identity_context = grant_synthetic_t2(
        session,
        clinic_id=clinic_id,
        patient_id=f"patient-{clinic_id}",
        channel=Channel.CALL,
        now=NOW,
        suffix=f"surface-{clinic_id}",
    )
    book_slot(
        session,
        clinic_id,
        patient_id=f"patient-{clinic_id}",
        outreach_job_id=f"job-{clinic_id}",
        slot_id=slot_id,
        now=NOW,
        require_staff_approval=True,
        identity_service=identity_service,
        identity_context=identity_context,
    )
    session.add(
        Interaction(
            id=f"interaction-{clinic_id}",
            clinic_id=clinic_id,
            outreach_job_id=f"job-{clinic_id}",
            channel=Channel.SMS,
            direction=InteractionDirection.OUTBOUND,
            content="private message body",
            outcome=InteractionOutcome.AUTO_HANDLED,
            occurred_at=NOW,
        )
    )


@pytest.fixture
def client(monkeypatch) -> TestClient:
    factory = _factory()
    with factory.begin() as session:
        for ordinal in range(2, 6):
            patient_id = f"patient-clinic-a-fixture-{ordinal}"
            session.add(
                Patient(
                    id=patient_id,
                    clinic_id="clinic-a",
                    source_ref=f"P-clinic-a-fixture-{ordinal}",
                    name=f"Synthetic Fixture Participant {ordinal}",
                )
            )
        programme = create_programme(
            session,
            clinic_id="clinic-a",
            programme_id="pilot-surface-fixture",
            environment="production",
            release_identity="sha256:surface-fixture",
        )
        mark_programme_dark(
            session,
            clinic_id="clinic-a",
            programme_id=programme.id,
            actor="operator:fixture",
            evidence_hash="d" * 64,
            now=NOW - timedelta(minutes=1),
        )
        participant_ids = ["patient-clinic-a"] + [
            f"patient-clinic-a-fixture-{ordinal}" for ordinal in range(2, 6)
        ]
        for patient_id in participant_ids:
            enroll_participant(
                session,
                clinic_id="clinic-a",
                programme_id=programme.id,
                patient_id=patient_id,
                now=NOW,
            )
        release_cumulative_limit(
            session,
            clinic_id="clinic-a",
            programme_id=programme.id,
            cumulative_limit=5,
            actor="operator:fixture",
            evidence_hash="a" * 64,
            now=NOW,
        )
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("CLINIC_RECALL_STAFF_CLINIC_ID", "clinic-a")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ACTOR", "staff:test")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "staff")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_OUTREACH_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_VOICE_ENABLED", "true")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_RECORDING_ENABLED", "false")
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT",
        (NOW - timedelta(seconds=5)).isoformat(),
    )
    monkeypatch.setenv("CLINIC_RECALL_PILOT_CONFIG_MAX_AGE_SECONDS", "60")
    monkeypatch.setenv("CLINIC_RECALL_PILOT_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_RELEASE_IDENTITY",
        "sha256:surface-fixture",
    )
    return TestClient(_app())


def test_queue_read_ignores_client_supplied_clinic_id(client: TestClient) -> None:
    response = client.get(
        "/api/v1/clinic-recall/queue?clinic_id=clinic-b",
        headers={"X-Clinic-ID": "clinic-b"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"]
    assert all("clinic-b" not in item["patient_name"] for item in payload["items"])
    assert "private message body" not in response.text


def test_operator_pilot_programme_lifecycle_is_tenant_scoped_and_minimized(
    monkeypatch,
) -> None:
    factory = _factory()
    with factory.begin() as session:
        for ordinal in range(2, 6):
            session.add(
                Patient(
                    id=f"patient-clinic-a-{ordinal}",
                    clinic_id="clinic-a",
                    source_ref=f"P-clinic-a-{ordinal}",
                    name=f"Synthetic Pilot Patient {ordinal}",
                )
            )
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("CLINIC_RECALL_STAFF_CLINIC_ID", "clinic-a")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ACTOR", "operator:test")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "operator")
    operator = TestClient(_app())

    created = operator.post(
        "/api/v1/clinic-recall/operator/pilot/programmes",
        json={
            "programme_id": "pilot-api-r1",
            "environment": "production",
            "release_identity": "sha256:api-release-r1",
        },
    )
    assert created.status_code == 200
    conflicting_id = operator.post(
        "/api/v1/clinic-recall/operator/pilot/programmes",
        json={
            "programme_id": "pilot-api-r1",
            "environment": "staging",
            "release_identity": "sha256:api-release-r2",
        },
    )
    assert conflicting_id.status_code == 409
    assert conflicting_id.json()["detail"] == "programme id already belongs to another release"
    patient_ids = ["patient-clinic-a"] + [
        f"patient-clinic-a-{ordinal}" for ordinal in range(2, 6)
    ]
    for ordinal, patient_id in enumerate(patient_ids, start=1):
        enrolled = operator.post(
            "/api/v1/clinic-recall/operator/pilot/programmes/pilot-api-r1/participants",
            json={"patient_id": patient_id},
        )
        assert enrolled.status_code == 200
        assert enrolled.json()["ordinal"] == ordinal
        assert "patient_id" not in enrolled.json()

    dark = operator.post(
        "/api/v1/clinic-recall/operator/pilot/programmes/pilot-api-r1/dark",
        json={"evidence_hash": "d" * 64},
    )

    released = operator.post(
        "/api/v1/clinic-recall/operator/pilot/programmes/pilot-api-r1/release",
        json={"cumulative_limit": 5, "evidence_hash": "a" * 64},
    )
    listed = operator.get("/api/v1/clinic-recall/operator/pilot/programmes")
    paused = operator.post(
        "/api/v1/clinic-recall/operator/pilot/programmes/pilot-api-r1/pause",
        json={"reason": "operator_pause"},
    )
    closed = operator.post(
        "/api/v1/clinic-recall/operator/pilot/programmes/pilot-api-r1/close",
        json={"reason": "pilot_complete"},
    )

    assert dark.status_code == 200
    assert dark.json()["state"] == "dark"
    assert released.status_code == 200
    assert released.json()["state"] == "active"
    assert released.json()["active_cumulative_limit"] == 5
    assert listed.status_code == 200
    assert listed.json()["programmes"][0]["participant_count"] == 5
    assert "patient" not in listed.text.lower()
    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"

    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "staff")
    forbidden = TestClient(_app()).get(
        "/api/v1/clinic-recall/operator/pilot/programmes"
    )
    assert forbidden.status_code == 403


def test_pilot_stop_dispatch_runs_immediate_bounded_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "src.clinic_recall.durable.recording_worker.run_runtime_batch",
        lambda **kwargs: calls.append(kwargs),
    )

    clinic_recall._dispatch_recording_stop_batch("clinic-a")

    assert len(calls) == 1
    assert calls[0]["clinic_id"] == "clinic-a"
    assert calls[0]["limit"] == 50
    assert str(calls[0]["worker_id"]).startswith("recording-pilot-stop-")


def test_pilot_stop_dispatch_contains_recoverable_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_batch(**_kwargs) -> None:
        raise RuntimeError("synthetic recording worker failure")

    monkeypatch.setattr(
        "src.clinic_recall.durable.recording_worker.run_runtime_batch",
        fail_batch,
    )

    clinic_recall._dispatch_recording_stop_batch("clinic-a")


def test_inbox_alias_uses_existing_staff_queue(client: TestClient) -> None:
    queue = client.get("/api/v1/clinic-recall/queue")
    inbox = client.get("/api/v1/clinic-recall/inbox?clinic_id=clinic-b")

    assert inbox.status_code == 200
    assert inbox.json() == queue.json()


def test_queue_resolve_rejects_path_encoded_cross_clinic_item(client: TestClient) -> None:
    response = client.post(
        "/api/v1/clinic-recall/queue/booking_action%3Abooking-action-clinic-b/resolve",
        headers={"X-Clinic-ID": "clinic-b"},
        json={"decision": "approve", "clinic_id": "clinic-b"},
    )

    assert response.status_code == 404


def test_queue_resolve_blocks_cross_clinic_idor(client: TestClient) -> None:
    response = client.post(
        "/api/v1/clinic-recall/queue/booking_action:booking-action-does-not-exist/resolve",
        json={"decision": "approve", "clinic_id": "clinic-b"},
    )

    assert response.status_code == 404


def test_queue_resolve_without_identity_policy_keeps_action_pending(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        clinic_recall,
        "job_gate_for_snapshot",
        lambda *_args: pytest.fail("staff confirmation must not require an active campaign"),
    )
    queue = client.get("/api/v1/clinic-recall/queue").json()["items"]
    booking_item = next(item for item in queue if item["kind"] == "booking_action")

    response = client.post(
        f"/api/v1/clinic-recall/queue/{booking_item['item_id']}/resolve",
        json={"decision": "approve"},
    )

    assert response.status_code == 200
    assert response.json()["booking_status"] == BookingActionStatus.PENDING.value
    assert response.json()["resolved"] is False
    assert response.json()["error"] == "identity_t2_required"
    assert response.json()["provider_confirmed"] is False
    assert response.json()["confirmation_sent"] is False


def test_inbox_acknowledge_marks_escalation_without_resolving_work(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        clinic_recall,
        "job_gate_for_snapshot",
        lambda *_args: pytest.fail("staff acknowledgement must not require an active campaign"),
    )
    factory = clinic_recall.get_sessionmaker()
    with factory.begin() as session:
        escalation = escalate_to_staff(
            session,
            "clinic-a",
            patient_id="patient-clinic-a",
            outreach_job_id="job-clinic-a",
            reason=EscalationReason.CLINICAL,
            now=NOW,
            context="Synthetic acknowledgement regression",
        )
        booking_states_before = list(
            session.execute(
                select(BookingAction.status).where(BookingAction.clinic_id == "clinic-a")
            ).scalars()
        )
        effect_ids_before = list(
            session.execute(
                select(ExternalEffect.id).where(ExternalEffect.clinic_id == "clinic-a")
            ).scalars()
        )

    inbox = client.get("/api/v1/clinic-recall/inbox").json()["items"]
    escalation_item = next(
        item
        for item in inbox
        if item["item_id"] == f"escalation:{escalation.escalation_id}"
    )
    assert escalation_item["status"] == EscalationStatus.OPEN.value

    first = client.post(
        f"/api/v1/clinic-recall/inbox/{escalation_item['item_id']}/acknowledge",
    )
    second = client.post(
        f"/api/v1/clinic-recall/inbox/{escalation_item['item_id']}/acknowledge",
    )

    assert first.status_code == 200
    assert first.json()["escalation_status"] == EscalationStatus.ACKNOWLEDGED.value
    assert first.json()["resolved"] is False
    assert first.json()["idempotent"] is False
    assert second.status_code == 200
    assert second.json()["escalation_status"] == EscalationStatus.ACKNOWLEDGED.value
    assert second.json()["resolved"] is False
    assert second.json()["idempotent"] is True

    with factory() as session:
        stored_escalation = session.get(Escalation, escalation.escalation_id)
        stored_job = session.get(OutreachJob, "job-clinic-a")
        booking_states_after = list(
            session.execute(
                select(BookingAction.status).where(BookingAction.clinic_id == "clinic-a")
            ).scalars()
        )
        effect_ids_after = list(
            session.execute(
                select(ExternalEffect.id).where(ExternalEffect.clinic_id == "clinic-a")
            ).scalars()
        )

    assert stored_escalation is not None
    assert stored_escalation.status == EscalationStatus.ACKNOWLEDGED
    assert stored_job is not None
    assert stored_job.state == OutreachState.ESCALATED
    assert booking_states_after == booking_states_before
    assert effect_ids_after == effect_ids_before


def test_booking_acknowledge_is_idempotent_and_never_decides_booking(
    client: TestClient,
) -> None:
    factory = clinic_recall.get_sessionmaker()
    inbox = client.get("/api/v1/clinic-recall/inbox").json()["items"]
    booking_item = next(item for item in inbox if item["kind"] == "booking_action")

    first = client.post(
        f"/api/v1/clinic-recall/inbox/{booking_item['item_id']}/acknowledge"
    )
    second = client.post(
        f"/api/v1/clinic-recall/inbox/{booking_item['item_id']}/acknowledge"
    )

    assert first.status_code == 200
    assert first.json()["acknowledged"] is True
    assert first.json()["resolved"] is False
    assert first.json()["booking_status"] == BookingActionStatus.PENDING.value
    assert first.json()["idempotent"] is False
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    with factory() as session:
        action = session.get(BookingAction, booking_item["item_id"].split(":", 1)[1])
        receipt = session.scalar(
            select(HandoffReceipt).where(HandoffReceipt.booking_action_id == action.id)
        )
    assert action is not None and action.status == BookingActionStatus.PENDING
    assert receipt is not None
    assert receipt.acknowledged_at is not None
    assert receipt.acknowledged_by == "staff:test"
    assert receipt.resolved_at is None


def test_inbound_task_acknowledge_and_direct_resolve_are_separate(
    client: TestClient,
) -> None:
    factory = clinic_recall.get_sessionmaker()
    with factory.begin() as session:
        callback = session.get(InboundStaffTask, "inbound-task-clinic-a")
        booking = session.get(InboundStaffTask, "inbound-text-task-clinic-a")
        assert callback is not None and booking is not None
        ensure_handoff_receipt(session, "clinic-a", callback, now=NOW)
        ensure_handoff_receipt(session, "clinic-a", booking, now=NOW)

    acknowledged = client.post(
        "/api/v1/clinic-recall/inbound-tasks/inbound-task-clinic-a/acknowledge"
    )
    resolved = client.post(
        "/api/v1/clinic-recall/inbound-tasks/inbound-text-task-clinic-a/resolve",
        json={"status": "resolved", "reason": "Handled by clinic staff"},
    )

    assert acknowledged.status_code == 200
    assert acknowledged.json()["status"] == InboundStaffTaskStatus.ACKNOWLEDGED.value
    assert resolved.status_code == 200
    assert resolved.json()["status"] == InboundStaffTaskStatus.RESOLVED.value
    with factory() as session:
        callback_receipt = session.scalar(
            select(HandoffReceipt).where(
                HandoffReceipt.inbound_staff_task_id == "inbound-task-clinic-a"
            )
        )
        resolved_receipt = session.scalar(
            select(HandoffReceipt).where(
                HandoffReceipt.inbound_staff_task_id
                == "inbound-text-task-clinic-a"
            )
        )
    assert callback_receipt is not None
    assert callback_receipt.acknowledged_at is not None
    assert callback_receipt.resolved_at is None
    assert resolved_receipt is not None
    assert resolved_receipt.acknowledged_at is not None
    assert resolved_receipt.resolved_at is not None
    assert resolved_receipt.acknowledged_by == "staff:test"
    assert resolved_receipt.resolved_by == "staff:test"


def test_exhausted_effect_handoff_is_visible_acknowledgeable_and_resolvable(
    client: TestClient,
) -> None:
    factory = clinic_recall.get_sessionmaker()
    with factory.begin() as session:
        source_effect = session.scalar(
            select(ExternalEffect).where(
                ExternalEffect.clinic_id == "clinic-a"
            )
        )
        assert source_effect is not None
        source_effect.state = ExternalEffectState.DEAD_LETTER
        session.flush()
        source_state = source_effect.state
        handoff, created = ensure_external_effect_handoff(
            session,
            source_effect,
            reason_code="retry_exhausted",
            now=NOW,
        )
        assert created is True

    inbox = client.get("/api/v1/clinic-recall/inbox").json()["items"]
    item = next(
        row
        for row in inbox
        if row["item_id"] == f"external_effect_handoff:{handoff.id}"
    )
    acknowledged = client.post(
        f"/api/v1/clinic-recall/inbox/{item['item_id']}/acknowledge"
    )
    resolved = client.post(
        f"/api/v1/clinic-recall/queue/{item['item_id']}/resolve",
        json={"decision": "resolve"},
    )

    assert acknowledged.status_code == 200
    assert acknowledged.json()["resolved"] is False
    assert resolved.status_code == 200
    assert resolved.json()["resolved"] is True
    with factory() as session:
        source = session.get(ExternalEffect, source_effect.id)
        owner = session.get(ExternalEffectHandoff, handoff.id)
        receipt = session.scalar(
            select(HandoffReceipt).where(
                HandoffReceipt.external_effect_handoff_id == handoff.id
            )
        )
    assert source is not None and source.state == source_state
    assert owner is not None and owner.status == "resolved"
    assert receipt is not None
    assert receipt.acknowledged_at is not None
    assert receipt.resolved_at is not None
    remaining = client.get("/api/v1/clinic-recall/inbox").json()["items"]
    assert item["item_id"] not in {row["item_id"] for row in remaining}


def test_cross_tenant_handoff_acknowledgement_returns_not_found(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/clinic-recall/inbox/external_effect_handoff:missing/acknowledge",
        headers={"X-Clinic-ID": "clinic-b"},
    )

    assert response.status_code == 404


def test_roi_read_and_csv_are_scoped_and_aggregate_only(client: TestClient) -> None:
    response = client.get(
        "/api/v1/clinic-recall/roi",
        params={"start": "2026-06-01T00:00:00Z", "end": "2026-07-01T00:00:00Z", "clinic_id": "clinic-b"},
        headers={"X-Clinic-ID": "clinic-b"},
    )
    csv_response = client.get(
        "/api/v1/clinic-recall/roi.csv",
        params={"start": "2026-06-01T00:00:00Z", "end": "2026-07-01T00:00:00Z", "clinic_id": "clinic-b"},
        headers={"X-Clinic-ID": "clinic-b"},
    )

    assert response.status_code == 200
    assert response.json()["contacted"] == 1
    assert "private message body" not in response.text
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "patient-clinic" not in csv_response.text


def test_campaign_settings_and_launch_use_server_clinic_scope(client: TestClient) -> None:
    settings = client.get("/api/v1/clinic-recall/campaign/settings?clinic_id=clinic-b")
    updated = client.put(
        "/api/v1/clinic-recall/campaign/settings",
        json={"clinic_id": "clinic-b", "daily_caps": 25, "contact_hours": {"start_hour": 10, "end_hour": 16}},
    )
    launched = client.post(
        "/api/v1/clinic-recall/campaigns/launch",
        json={"clinic_id": "clinic-b", "now": NOW.isoformat()},
    )

    assert settings.status_code == 200
    assert updated.status_code == 200
    assert updated.json()["daily_caps"] == 25
    assert launched.status_code == 200
    assert launched.json()["candidate_queue"]["detected_total"] >= 1


def test_campaign_launch_creates_review_pending_batch_and_staff_can_approve_pause(
    client: TestClient,
) -> None:
    launched = client.post(
        "/api/v1/clinic-recall/campaigns/launch",
        json={"now": NOW.isoformat()},
    )
    campaigns = client.get("/api/v1/clinic-recall/campaigns")

    assert launched.status_code == 200
    assert campaigns.status_code == 200
    draft_campaign = next(
        campaign
        for campaign in campaigns.json()["campaigns"]
        if campaign["id"] == "campaign-detection-clinic-a"
    )
    assert draft_campaign["status"] == CampaignStatus.DRAFT.value
    assert draft_campaign["is_approvable"] is True

    approved = client.post(
        "/api/v1/clinic-recall/campaigns/campaign-detection-clinic-a/approve"
    )
    paused = client.post("/api/v1/clinic-recall/campaigns/campaign-detection-clinic-a/pause")
    missing = client.post("/api/v1/clinic-recall/campaigns/campaign-detection-clinic-b/approve")

    assert approved.status_code == 200
    assert approved.json()["status"] == CampaignStatus.ACTIVE.value
    assert paused.status_code == 200
    assert paused.json()["status"] == CampaignStatus.PAUSED.value
    assert missing.status_code == 404


def test_voice_fallback_endpoint_is_operator_only_and_provider_free(
    monkeypatch,
) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(
        clinic_recall,
        "get_privacy_sessionmaker",
        lambda: factory,
    )
    monkeypatch.setattr(
        clinic_recall,
        "build_call_initiator",
        lambda: (_ for _ in ()).throw(AssertionError("provider must not be constructed")),
        raising=False,
    )
    monkeypatch.setenv("CLINIC_RECALL_STAFF_CLINIC_ID", "clinic-a")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ACTOR", "operator:test")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "operator")
    with factory() as session:
        session.add(
            OutreachJob(
                id="voice-fallback-job-clinic-a",
                clinic_id="clinic-a",
                campaign_id="campaign-clinic-a",
                patient_id="patient-clinic-a",
                appointment_id="appointment-clinic-a",
                channel=Channel.SMS,
                state=OutreachState.NO_REPLY,
            )
        )
        session.commit()

    response = TestClient(_app()).post(
        "/api/v1/clinic-recall/voice/fallback/run",
        json={"now": NOW.isoformat()},
    )

    assert response.status_code == 200
    summary = response.json()["voice_fallback"]
    assert summary["calls_initiated"] == 0
    assert summary["calls_enqueued"] == 0
    assert sum(summary["skipped"].values()) == 1


def test_voice_fallback_endpoint_rejects_non_operator(client: TestClient) -> None:
    response = client.post(
        "/api/v1/clinic-recall/voice/fallback/run",
        json={"now": NOW.isoformat()},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Clinic Recall operator access required"


def test_prompt_proposal_endpoint_returns_diff_without_writing(monkeypatch, tmp_path) -> None:
    prompt_path = tmp_path / "recall-agent.prompt.md"
    prompt_path.write_text("Current safe prompt\n", encoding="utf-8")
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("CLINIC_RECALL_STAFF_CLINIC_ID", "clinic-a")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ACTOR", "operator:test")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "operator")
    monkeypatch.setenv("RECALL_AGENT_PROMPT_PATH", str(prompt_path))

    response = TestClient(_app()).post(
        "/api/v1/clinic-recall/operator/prompt-proposal",
        json={"proposed_prompt": "Current safe prompt\nAdd a safer clinic tone."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["gate_required"] is True
    assert "---" in payload["diff"]
    assert "+Add a safer clinic tone." in payload["diff"]
    assert prompt_path.read_text(encoding="utf-8") == "Current safe prompt\n"


def test_prompt_proposal_endpoint_rejects_non_operator(client: TestClient) -> None:
    response = client.post(
        "/api/v1/clinic-recall/operator/prompt-proposal",
        json={"proposed_prompt": "Current safe prompt\nAdd a safer clinic tone."},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Clinic Recall operator access required"


def test_prompt_proposals_are_persisted_scoped_and_do_not_write_prompt_file(
    monkeypatch,
    tmp_path,
) -> None:
    prompt_path = tmp_path / "recall-agent.prompt.md"
    prompt_path.write_text("Current governed prompt\n", encoding="utf-8")
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("RECALL_AGENT_PROMPT_PATH", str(prompt_path))
    monkeypatch.setenv("CLINIC_RECALL_STAFF_CLINIC_ID", "clinic-a")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ACTOR", "operator:test")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "operator")
    client = TestClient(_app())

    created = client.post(
        "/api/v1/clinic-recall/operator/prompt-proposals",
        json={"proposed_prompt": "Current governed prompt\nAdd calmer clinic tone."},
    )
    listed = client.get("/api/v1/clinic-recall/operator/prompt-proposals")
    proposal_id = created.json()["id"]
    fetched = client.get(f"/api/v1/clinic-recall/operator/prompt-proposals/{proposal_id}")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_CLINIC_ID", "clinic-b")
    cross_clinic_list = client.get("/api/v1/clinic-recall/operator/prompt-proposals")
    cross_clinic_fetch = client.get(f"/api/v1/clinic-recall/operator/prompt-proposals/{proposal_id}")

    assert created.status_code == 200
    assert created.json()["status"] == "submitted"
    assert created.json()["gate_required"] is True
    assert "+Add calmer clinic tone." in created.json()["diff"]
    assert listed.status_code == 200
    assert [proposal["id"] for proposal in listed.json()["proposals"]] == [proposal_id]
    assert fetched.status_code == 200
    assert fetched.json()["id"] == proposal_id
    assert cross_clinic_list.status_code == 200
    assert cross_clinic_list.json()["proposals"] == []
    assert cross_clinic_fetch.status_code == 404
    assert prompt_path.read_text(encoding="utf-8") == "Current governed prompt\n"


def test_prompt_proposals_reject_non_operator(client: TestClient) -> None:
    response = client.get("/api/v1/clinic-recall/operator/prompt-proposals")

    assert response.status_code == 403
    assert response.json()["detail"] == "Clinic Recall operator access required"


def test_operator_script_templates_are_persisted_and_role_gated(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("CLINIC_RECALL_STAFF_CLINIC_ID", "clinic-a")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ACTOR", "operator:test")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "operator")
    client = TestClient(_app())

    initial = client.get("/api/v1/clinic-recall/operator/script-templates")
    updated = client.put(
        "/api/v1/clinic-recall/operator/script-templates",
        json={"templates": {"missed": "Let us help you rebook safely."}},
    )
    fetched = client.get("/api/v1/clinic-recall/operator/script-templates")

    assert initial.status_code == 200
    assert "missed" in initial.json()["templates"]
    assert updated.status_code == 200
    assert updated.json()["templates"] == {"missed": "Let us help you rebook safely."}
    assert fetched.json()["templates"] == {"missed": "Let us help you rebook safely."}


def test_operator_script_templates_reject_non_operator(client: TestClient) -> None:
    response = client.get("/api/v1/clinic-recall/operator/script-templates")

    assert response.status_code == 403
    assert response.json()["detail"] == "Clinic Recall operator access required"


def test_operator_voice_persona_is_persisted(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("CLINIC_RECALL_STAFF_CLINIC_ID", "clinic-a")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ACTOR", "operator:test")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "operator")
    client = TestClient(_app())

    initial = client.get("/api/v1/clinic-recall/operator/voice-persona")
    updated = client.put(
        "/api/v1/clinic-recall/operator/voice-persona",
        json={"display_name": "Clinic Recall", "tone": "calm, warm, brief", "voice_name": "Ada"},
    )
    fetched = client.get("/api/v1/clinic-recall/operator/voice-persona")

    assert initial.status_code == 200
    assert initial.json()["display_name"] == "clinic-a Clinic"
    assert updated.status_code == 200
    assert updated.json() == {
        "display_name": "Clinic Recall",
        "tone": "calm, warm, brief",
        "voice_name": "Ada",
    }
    assert fetched.json() == updated.json()


def test_outbox_preview_is_scoped_and_uses_deterministic_templates(client: TestClient) -> None:
    client.post(
        "/api/v1/clinic-recall/campaigns/launch",
        json={"clinic_id": "clinic-b", "now": NOW.isoformat()},
    )

    response = client.get(
        "/api/v1/clinic-recall/outbox",
        params={"clinic_id": "clinic-b", "now": NOW.isoformat()},
        headers={"X-Clinic-ID": "clinic-b"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["campaign_id"] == "campaign-detection-clinic-a"
    assert item["campaign_status"] == CampaignStatus.DRAFT.value
    assert item["template_id"] == "clinic_recall_sms_v1"
    assert item["eligible_now"] is True
    assert item["skip_reason"] is None
    assert item["can_send_after_approval"] is True
    assert "Reply STOP to opt out" in item["message_preview"]
    assert "clinic-b Patient" not in response.text


def test_outbox_reports_eligibility_skip_reason(
    client: TestClient,
    monkeypatch,
) -> None:
    client.post(
        "/api/v1/clinic-recall/campaigns/launch",
        json={"now": NOW.isoformat()},
    )
    monkeypatch.setenv(
        "CLINIC_RECALL_PILOT_CONFIG_REFRESHED_AT",
        "2026-06-27T02:59:55Z",
    )

    response = client.get(
        "/api/v1/clinic-recall/outbox",
        params={"now": "2026-06-27T03:00:00Z"},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["eligible_now"] is False
    assert item["skip_reason"] == "outside_contact_hours"
    assert item["can_send_after_approval"] is False


def test_interactions_timeline_is_scoped_and_minimised(client: TestClient) -> None:
    response = client.get(
        "/api/v1/clinic-recall/interactions",
        params={"clinic_id": "clinic-b"},
        headers={"X-Clinic-ID": "clinic-b"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["channel"] == Channel.SMS.value
    assert item["direction"] == InteractionDirection.OUTBOUND.value
    assert item["outcome"] == InteractionOutcome.AUTO_HANDLED.value
    assert item["outreach_job_id"] == "job-clinic-a"
    assert item["content_preview"] is None
    assert "private message body" not in response.text
    assert "clinic-b" not in response.text


def test_monitor_is_scoped_and_aggregate_only(client: TestClient) -> None:
    response = client.get(
        "/api/v1/clinic-recall/monitor",
        params={"clinic_id": "clinic-b"},
        headers={"X-Clinic-ID": "clinic-b"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["open_queue_count"] == 1
    assert payload["queued_outbox_count"] == 0
    assert payload["active_campaigns"] == 1
    assert payload["voice_fallback_summary"]["call_jobs_by_state"] == {"escalated": 1}
    assert "private message body" not in response.text
    assert "clinic-b Patient" not in response.text


def test_inbound_phone_surface_endpoints_are_scoped_and_redacted(client: TestClient) -> None:
    numbers = client.get("/api/v1/clinic-recall/phone-numbers?clinic_id=clinic-b")
    calls = client.get("/api/v1/clinic-recall/inbound-calls?clinic_id=clinic-b")
    messages = client.get("/api/v1/clinic-recall/inbound-messages?clinic_id=clinic-b")
    tasks = client.get("/api/v1/clinic-recall/inbound-tasks?clinic_id=clinic-b")
    metrics = client.get("/api/v1/clinic-recall/inbound-metrics?clinic_id=clinic-b")

    assert numbers.status_code == 200
    assert numbers.json()["items"] == [
        {
            "id": "phone-clinic-a",
            "provider": "twilio",
            "phone_number": "+1555000a",
            "purpose": "inbound",
            "status": "active",
            "webhook_url": "/api/v1/voice/twilio/twiml",
            "test_status": "green",
        }
    ]
    assert calls.status_code == 200
    assert calls.json()["items"][0]["id"] == "inbound-call-clinic-a"
    assert calls.json()["items"][0]["caller_number_redacted"] == "hash:-asecret"
    assert "clinic-b" not in calls.text
    assert messages.status_code == 200
    assert messages.json()["items"][0]["id"] == "inbound-message-clinic-a"
    assert messages.json()["items"][0]["from_number_redacted"] == "hash:xtsecret"
    assert "messagehash" not in messages.text
    assert "clinic-b" not in messages.text
    assert tasks.status_code == 200
    assert {item["source"] for item in tasks.json()["items"]} == {"call", "sms"}
    assert metrics.status_code == 200
    assert metrics.json()["calls_total"] == 1
    assert metrics.json()["texts_total"] == 1
    assert metrics.json()["text_booking_requests_open"] == 1
    assert metrics.json()["callbacks_open"] == 1


def test_call_ledger_status_is_operator_only_scoped_and_minimized(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert client.get("/api/v1/clinic-recall/call-records").status_code == 403

    factory = clinic_recall.get_sessionmaker()
    with factory.begin() as session:
        session.add_all(
            [
                CallRecord(
                    id="callrec-surface-inbound-a",
                    clinic_id="clinic-a",
                    inbound_call_id="inbound-call-clinic-a",
                    provider=ClinicPhoneProvider.TWILIO,
                    provider_call_id="CA" + "a" * 32,
                    direction=InteractionDirection.INBOUND,
                    scenario="inbound_clinic",
                    started_at=NOW,
                    consent_state=RecordingConsentState.DECLINED,
                    consent_asked_at=NOW,
                    consent_decided_at=NOW,
                    consent_decision_source=RecordingConsentSource.DTMF,
                    consent_version="synthetic-pr09-v1",
                    recording_status=CallRecordingStatus.NONE,
                    deletion_state=RecordingDeletionState.NOT_REQUESTED,
                ),
                CallRecord(
                    id="callrec-surface-outbound-a",
                    clinic_id="clinic-a",
                    patient_id="patient-clinic-a",
                    provider=ClinicPhoneProvider.TWILIO,
                    provider_call_id="CA" + "b" * 32,
                    direction=InteractionDirection.OUTBOUND,
                    scenario="rebooking",
                    started_at=NOW,
                    consent_state=RecordingConsentState.GRANTED,
                    consent_asked_at=NOW,
                    consent_decided_at=NOW,
                    consent_decision_source=RecordingConsentSource.SPEECH,
                    consent_version="synthetic-pr09-v1",
                    recording_requested_at=NOW,
                    recording_started_at=NOW,
                    recording_status=CallRecordingStatus.IN_PROGRESS,
                    recording_sid="RE" + "c" * 32,
                    recording_blob_path="private/recording.wav",
                    transcript=[{"role": "user", "text": "private transcript"}],
                    deletion_state=RecordingDeletionState.NOT_REQUESTED,
                ),
                CallRecord(
                    id="callrec-surface-b",
                    clinic_id="clinic-b",
                    patient_id="patient-clinic-b",
                    provider=ClinicPhoneProvider.TWILIO,
                    provider_call_id="CA" + "d" * 32,
                    direction=InteractionDirection.OUTBOUND,
                    scenario="rebooking",
                    started_at=NOW,
                    consent_state=RecordingConsentState.NOT_ASKED,
                    recording_status=CallRecordingStatus.NONE,
                    deletion_state=RecordingDeletionState.NOT_REQUESTED,
                ),
            ]
        )
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "operator")

    response = client.get(
        "/api/v1/clinic-recall/call-records?clinic_id=clinic-b",
        headers={"X-Clinic-ID": "clinic-b"},
    )

    assert response.status_code == 200
    assert {item["id"] for item in response.json()["items"]} == {
        "callrec-surface-inbound-a",
        "callrec-surface-outbound-a",
    }
    outbound = next(
        item for item in response.json()["items"] if item["direction"] == "outbound"
    )
    assert outbound["patient_linked"] is True
    assert outbound["provider_call_bound"] is True
    assert outbound["recording_identity_bound"] is True
    assert outbound["consent_state"] == "granted"
    assert outbound["recording_status"] == "in_progress"
    assert "patient_id" not in outbound
    assert "provider_call_id" not in outbound
    assert "recording_sid" not in outbound
    assert "transcript" not in outbound
    assert "blob" not in response.text.lower()
    assert "private transcript" not in response.text
    assert "callrec-surface-b" not in response.text


def test_inbound_task_resolution_and_config_are_role_gated(client: TestClient, monkeypatch) -> None:
    resolved = client.post(
        "/api/v1/clinic-recall/inbound-tasks/inbound-task-clinic-a/resolve",
        json={"status": "resolved", "reason": "called back"},
    )
    blocked_config = client.put(
        "/api/v1/clinic-recall/inbound-config",
        json={"greeting": "Hello from reception"},
    )

    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert blocked_config.status_code == 403

    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "operator")
    operator_client = TestClient(_app())
    updated_config = operator_client.put(
        "/api/v1/clinic-recall/inbound-config",
        json={"greeting": "Hello from reception", "callback_sla_hours": 2},
    )
    status_update = operator_client.post(
        "/api/v1/clinic-recall/phone-numbers/phone-clinic-a/status",
        params={"status": "inactive"},
    )

    assert updated_config.status_code == 200
    assert updated_config.json()["greeting"] == "Hello from reception"
    assert updated_config.json()["callback_sla_hours"] == 2
    assert "recording_enabled" not in updated_config.json()
    recording_blocked = operator_client.put(
        "/api/v1/clinic-recall/inbound-config",
        json={"recording_enabled": True},
    )
    assert recording_blocked.status_code == 409
    assert recording_blocked.json()["detail"] == (
        "Recording remains off pending approved wording, privacy, and carrier qualification"
    )
    assert status_update.status_code == 200
    assert status_update.json()["status"] == "inactive"


def test_surfaces_require_staff_context(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ACTOR", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ROLES", raising=False)

    response = TestClient(_app()).get("/api/v1/clinic-recall/queue")

    assert response.status_code == 403


def test_surfaces_use_persisted_easyauth_mapping_before_env_identity_map(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ACTOR", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ROLES", raising=False)
    monkeypatch.setenv(
        "CLINIC_RECALL_IDENTITY_MAP",
        json.dumps(
            {
                "staff@example.test": {
                    "clinic_id": "clinic-a",
                    "actor": "staff:env-map",
                    "roles": ["clinic_staff"],
                }
            }
        ),
    )
    with factory() as session:
        session.add(
            ClinicIdentityMapping(
                id="identity-staff-example",
                clinic_id="clinic-b",
                subject="staff@example.test",
                email="staff@example.test",
                roles=["clinic_staff"],
                status="active",
            )
        )
        session.commit()

    response = TestClient(_app()).get(
        "/api/v1/clinic-recall/queue?clinic_id=clinic-a",
        headers={**_easy_auth_header(), "X-Clinic-ID": "clinic-a"},
    )

    assert response.status_code == 200
    assert "clinic-b" in response.text
    assert "clinic-a Patient" not in response.text


def test_surfaces_fail_closed_for_ambiguous_persisted_easyauth_mapping(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ACTOR", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ROLES", raising=False)
    with factory() as session:
        session.add_all(
            [
                ClinicIdentityMapping(
                    id="identity-staff-subject",
                    clinic_id="clinic-a",
                    subject="staff@example.test",
                    roles=["clinic_staff"],
                    status="active",
                ),
                ClinicIdentityMapping(
                    id="identity-staff-name",
                    clinic_id="clinic-b",
                    subject="clinic staff",
                    roles=["clinic_staff"],
                    status="active",
                ),
            ]
        )
        session.commit()

    response = TestClient(_app()).get(
        "/api/v1/clinic-recall/queue",
        headers=_easy_auth_header(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Clinic Recall identity mapping is ambiguous"


def test_email_only_mapping_does_not_auto_grant_access(monkeypatch) -> None:
    """Email is an admin-confirmed suggestion, never an auto-grant path."""
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ACTOR", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ROLES", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_IDENTITY_MAP", raising=False)
    with factory() as session:
        session.add(
            ClinicIdentityMapping(
                id="identity-email-only",
                clinic_id="clinic-a",
                subject="entra-oid-not-in-principal",
                email="staff@example.test",
                roles=["clinic_staff"],
                status="active",
            )
        )
        session.commit()

    response = TestClient(_app()).get(
        "/api/v1/clinic-recall/queue",
        headers=_easy_auth_header(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Clinic Recall identity is not mapped"


def test_google_identity_fails_closed_against_entra_mapping_with_same_email(monkeypatch) -> None:
    """A Google login must never inherit an Entra mapping via a shared email."""
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ACTOR", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ROLES", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_IDENTITY_MAP", raising=False)
    with factory() as session:
        session.add(
            ClinicIdentityMapping(
                id="identity-entra-staff",
                clinic_id="clinic-a",
                provider="aad",
                subject="staff@example.test",
                email="staff@example.test",
                roles=["clinic_staff"],
                status="active",
            )
        )
        session.commit()

    response = TestClient(_app()).get(
        "/api/v1/clinic-recall/queue",
        headers=_easy_auth_header(provider="google"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Clinic Recall identity is not mapped"


def test_google_identity_maps_via_provider_scoped_subject(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ACTOR", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ROLES", raising=False)
    with factory() as session:
        session.add(
            ClinicIdentityMapping(
                id="identity-google-staff",
                clinic_id="clinic-b",
                provider="google",
                subject="google-sub-1234567890",
                email="staff@example.test",
                roles=["clinic_staff"],
                status="active",
            )
        )
        session.commit()

    response = TestClient(_app()).get(
        "/api/v1/clinic-recall/queue",
        headers=_easy_auth_header(provider="google", user_id="google-sub-1234567890"),
    )

    assert response.status_code == 200
    assert "clinic-b" in response.text
    assert "clinic-a Patient" not in response.text


def test_same_subject_string_never_collides_across_providers(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ACTOR", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ROLES", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_IDENTITY_MAP", raising=False)
    with factory() as session:
        session.add(
            ClinicIdentityMapping(
                id="identity-entra-collide",
                clinic_id="clinic-a",
                provider="aad",
                subject="collide-id-000",
                roles=["clinic_staff"],
                status="active",
            )
        )
        session.commit()
    client = TestClient(_app())

    google_login = client.get(
        "/api/v1/clinic-recall/queue",
        headers=_easy_auth_header(provider="google", user_id="collide-id-000"),
    )
    entra_login = client.get(
        "/api/v1/clinic-recall/queue",
        headers=_easy_auth_header(user_id="collide-id-000"),
    )

    assert google_login.status_code == 403
    assert google_login.json()["detail"] == "Clinic Recall identity is not mapped"
    assert entra_login.status_code == 200
    assert "clinic-a" in entra_login.text


def test_self_serve_signup_is_feature_flagged_and_creates_pending_clinic(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ACTOR", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ROLES", raising=False)

    disabled = TestClient(_app()).post(
        "/api/v1/clinic-recall/signup",
        json={"clinic_name": "New Smile Clinic", "contact_email": "hello@example.test"},
    )

    monkeypatch.setenv("ENABLE_SELF_SERVE_SIGNUP", "true")
    created = TestClient(_app()).post(
        "/api/v1/clinic-recall/signup",
        json={"clinic_name": "New Smile Clinic", "contact_email": "hello@example.test"},
    )

    assert disabled.status_code == 403
    assert created.status_code == 200
    payload = created.json()
    assert payload == {
        "clinic_id": "clinic-new-smile-clinic",
        "status": "pending",
        "onboarding_next": "connect_data",
    }
    with factory() as session:
        clinic = session.get(Clinic, "clinic-new-smile-clinic")
        assert clinic is not None
        assert clinic.consent_policy["signup_status"] == "pending"
        assert clinic.consent_policy["outreach_enabled"] is False
        assert clinic.daily_caps == 25
        assert session.query(ClinicIdentityMapping).count() == 0

    privileged = TestClient(_app()).get("/api/v1/clinic-recall/onboarding")

    assert privileged.status_code == 403


def test_self_serve_signup_with_easyauth_maps_creator_and_onboarding_round_trips(
    monkeypatch,
) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ACTOR", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ROLES", raising=False)
    monkeypatch.setenv("ENABLE_SELF_SERVE_SIGNUP", "true")
    client = TestClient(_app())
    headers = _easy_auth_header("creator@example.test")

    created = client.post(
        "/api/v1/clinic-recall/signup",
        json={"clinic_name": "Mapped Creator Clinic", "contact_email": "creator@example.test"},
        headers=headers,
    )
    blocked_launch = client.post(
        "/api/v1/clinic-recall/campaigns/launch",
        json={"now": NOW.isoformat()},
        headers=headers,
    )
    onboarding = client.get("/api/v1/clinic-recall/onboarding", headers=headers)
    # PR-08: a client cannot mark connect_data complete; the value is ignored.
    ignored_step = client.put(
        "/api/v1/clinic-recall/onboarding",
        json={"completed_step": "connect_data"},
        headers=headers,
    )
    # Durable server-side evidence (one completed import batch) completes it.
    with factory() as evidence_session:
        evidence_session.add(
            ImportBatch(
                id="impb-onboarding-evidence",
                clinic_id="clinic-mapped-creator-clinic",
                state=ImportBatchState.COMPLETED,
                file_sha256="a" * 64,
                validation_summary_sha256="b" * 64,
                schema_version="wulo-csv-v1",
                source_system=SourceSystem.CSV,
                export_at=NOW,
                preview_requested_at=NOW,
                preview_actor="staff:creator@example.test",
                preview_expires_at=NOW + timedelta(minutes=30),
                preview_upload_disposed_at=NOW,
                approved_at=NOW,
                approved_by="staff:creator@example.test",
                approval_upload_disposed_at=NOW,
                completed_at=NOW,
            )
        )
        evidence_session.commit()
    first_step = client.get("/api/v1/clinic-recall/onboarding", headers=headers)
    setup_complete = client.put(
        "/api/v1/clinic-recall/onboarding",
        json={
            "onboarding_steps": {
                "confirm_number": True,
                "choose_script": True,
                "set_rules": True,
                "first_campaign": True,
            }
        },
        headers=headers,
    )
    enabled = client.put(
        "/api/v1/clinic-recall/onboarding",
        json={"outreach_enabled": True},
        headers=headers,
    )
    launched = client.post(
        "/api/v1/clinic-recall/campaigns/launch",
        json={"now": NOW.isoformat()},
        headers=headers,
    )

    assert created.status_code == 200
    assert created.json()["clinic_id"] == "clinic-mapped-creator-clinic"
    assert blocked_launch.status_code == 409
    assert onboarding.status_code == 200
    assert onboarding.json() == {
        "status": "pending",
        "onboarding_required": True,
        "onboarding_step": "connect_data",
        "onboarding_steps": {
            "connect_data": False,
            "confirm_number": False,
            "choose_script": False,
            "set_rules": False,
            "first_campaign": False,
        },
        "outreach_enabled": False,
    }
    assert first_step.status_code == 200
    assert ignored_step.status_code == 200
    assert ignored_step.json()["onboarding_steps"]["connect_data"] is False
    assert ignored_step.json()["onboarding_step"] == "connect_data"
    assert first_step.json()["onboarding_steps"]["connect_data"] is True
    assert first_step.json()["onboarding_step"] == "confirm_number"
    assert setup_complete.status_code == 200
    assert setup_complete.json()["status"] == "setup_complete"
    assert setup_complete.json()["outreach_enabled"] is False
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "active"
    assert enabled.json()["outreach_enabled"] is True
    assert launched.status_code == 200
    assert launched.json()["candidate_queue"]["detected_total"] == 0
    with factory() as session:
        mapping = session.query(ClinicIdentityMapping).filter_by(email="creator@example.test").one()
        assert mapping.clinic_id == "clinic-mapped-creator-clinic"
        assert mapping.roles == ["staff", "operator"]
        assert mapping.provider == "aad"


def test_self_serve_signup_records_google_provider_on_mapping(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("ENABLE_SELF_SERVE_SIGNUP", "true")

    created = TestClient(_app()).post(
        "/api/v1/clinic-recall/signup",
        json={"clinic_name": "Google Creator Clinic", "contact_email": "gcreator@example.test"},
        headers=_easy_auth_header("gcreator@example.test", provider="google", user_id="google-sub-42"),
    )

    assert created.status_code == 200
    with factory() as session:
        mapping = session.query(ClinicIdentityMapping).filter_by(email="gcreator@example.test").one()
        assert mapping.provider == "google"
        assert mapping.clinic_id == "clinic-google-creator-clinic"


def test_surfaces_use_easyauth_identity_mapping_and_ignore_client_clinic_id(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ACTOR", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ROLES", raising=False)
    monkeypatch.setenv(
        "CLINIC_RECALL_IDENTITY_MAP",
        json.dumps(
            {
                "staff@example.test": {
                    "clinic_id": "clinic-a",
                    "actor": "staff:easyauth",
                    "roles": ["clinic_staff"],
                }
            }
        ),
    )

    response = TestClient(_app()).get(
        "/api/v1/clinic-recall/queue?clinic_id=clinic-b",
        headers={**_easy_auth_header(), "X-Clinic-ID": "clinic-b"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"]
    assert all("clinic-b" not in item["patient_name"] for item in payload["items"])


def test_surfaces_reject_unmapped_easyauth_identity(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_CLINIC_ID", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ACTOR", raising=False)
    monkeypatch.delenv("CLINIC_RECALL_STAFF_ROLES", raising=False)
    monkeypatch.setenv(
        "CLINIC_RECALL_IDENTITY_MAP",
        json.dumps({"other@example.test": {"clinic_id": "clinic-a", "roles": ["clinic_staff"]}}),
    )

    response = TestClient(_app()).get(
        "/api/v1/clinic-recall/queue?clinic_id=clinic-a",
        headers=_easy_auth_header("staff@example.test"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Clinic Recall identity is not mapped"


def test_erase_patient_endpoint_creates_minimized_operator_rights_request(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(
        clinic_recall,
        "get_privacy_sessionmaker",
        lambda: factory,
    )
    monkeypatch.setenv("CLINIC_RECALL_STAFF_CLINIC_ID", "clinic-a")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ACTOR", "staff:test")
    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "staff")
    monkeypatch.setenv("CLINIC_RECALL_RIGHTS_HMAC_KEY_VERSION", "tests-v1")
    monkeypatch.setenv("CLINIC_RECALL_RIGHTS_HMAC_KEY", "tests-only-rights-secret")
    monkeypatch.setenv("CLINIC_RECALL_RIGHTS_POLICY_VERSION", "tests-policy-v1")
    monkeypatch.setenv("CLINIC_RECALL_RIGHTS_APPROVAL_EVIDENCE_SHA256", "a" * 64)
    monkeypatch.setenv("CLINIC_RECALL_RIGHTS_REQUEST_DUE_SECONDS", "3600")
    client = TestClient(_app())

    forbidden = client.post(
        "/api/v1/clinic-recall/patients/patient-clinic-a/erase",
        json={
            "confirm_token": erasure_confirm_token("patient-clinic-a"),
            "clinic_id": "clinic-b",
        },
    )
    assert forbidden.status_code == 403
    assert client.get("/api/v1/clinic-recall/rights/operations").status_code == 403

    monkeypatch.setenv("CLINIC_RECALL_STAFF_ROLES", "operator")
    client = TestClient(_app())
    rejected = client.post(
        "/api/v1/clinic-recall/patients/patient-clinic-a/erase",
        json={"confirm_token": "wrong", "clinic_id": "clinic-b"},
    )
    requested = client.post(
        "/api/v1/clinic-recall/patients/patient-clinic-a/erase",
        json={
            "confirm_token": erasure_confirm_token("patient-clinic-a"),
            "clinic_id": "clinic-b",
        },
    )

    assert rejected.status_code == 409
    assert requested.status_code == 202
    body = requested.json()
    assert set(body) == {"request_id", "state", "created", "target_count", "due_at"}
    assert body["state"] == "frozen"
    assert body["created"] is True
    assert body["target_count"] > 0
    assert "patient" not in json.dumps(body).lower()

    status = client.get(f"/api/v1/clinic-recall/rights/{body['request_id']}")
    assert status.status_code == 200
    status_body = status.json()
    assert set(status_body) == {
        "request_id",
        "state",
        "target_count",
        "pending_count",
        "verified_count",
        "residual_count",
        "unapproved_residual_count",
        "overdue_count",
        "requested_at",
        "due_at",
        "completed_at",
    }
    assert "patient" not in json.dumps(status_body).lower()

    operations = client.get("/api/v1/clinic-recall/rights/operations")
    assert operations.status_code == 200
    operations_body = operations.json()
    assert set(operations_body) == {
        "request_count",
        "incomplete_request_count",
        "target_count",
        "pending_count",
        "reconcile_required_count",
        "handoff_count",
        "unapproved_residual_count",
        "overdue_count",
        "zero_overdue",
        "ready",
    }
    assert operations_body["request_count"] == 1
    assert operations_body["target_count"] == body["target_count"]
    assert operations_body["zero_overdue"] is True
    assert operations_body["handoff_count"] == 0
    assert operations_body["ready"] is False
    assert "patient" not in json.dumps(operations_body).lower()
    with factory() as session:
        assert session.get(Patient, "patient-clinic-a") is not None
        assert session.get(Patient, "patient-clinic-b") is not None
