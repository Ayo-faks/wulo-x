"""HTTP integration tests for SMS webhooks -> Clinic Recall inbound routing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from apps.artagent.backend.api.v1.endpoints import sms
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall import inbound_messages
from src.clinic_recall.availability import AvailabilitySlotInput, upsert_availability_slots
from src.clinic_recall.enums import (
    BookingActionStatus,
    CampaignStatus,
    CampaignType,
    Channel,
    ClinicPhoneProvider,
    ClinicPhonePurpose,
    ClinicPhoneStatus,
    EscalationReason,
    InboundStaffTaskKind,
    OutreachState,
)
from src.clinic_recall.inbound_identity import resolve_single_inbound_patient_id
from src.clinic_recall.inbound_text_agent import InboundTextIntent
from src.clinic_recall.models import (
    Appointment,
    Base,
    BookingAction,
    Campaign,
    Clinic,
    ClinicPhoneNumber,
    Escalation,
    InboundMessage,
    InboundStaffTask,
    OutreachJob,
    Patient,
)
from src.clinic_recall.rights import (
    RightsPolicy,
    SubjectKey,
    SubjectKeyring,
    request_patient_erasure,
)

from tests.identity_evidence_support import grant_synthetic_t2


def _sms_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(sms.router, prefix="/api/v1/sms")
    return app


def _session_factory(*, include_job: bool = True) -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        _seed(session, include_job=include_job)
        session.commit()
    return factory


def _enable_synthetic_sms_t2(monkeypatch, factory, suffix: str) -> None:
    with factory() as session:
        identity_service, identity_context = grant_synthetic_t2(
            session,
            clinic_id="clinic-webhook",
            patient_id="patient-webhook",
            channel=Channel.SMS,
            now=datetime.now(UTC),
            suffix=f"sms-webhook-{suffix}",
        )
        session.commit()
    monkeypatch.setattr(
        sms,
        "sms_identity_dependencies",
        lambda _route, _from_address, _now: (
            identity_service,
            identity_context,
        ),
    )


def _upsert_fresh_slot(
    session: Session,
    source_ref: str,
    start_at: datetime,
):
    observed_at = datetime.now(UTC)
    return upsert_availability_slots(
        session,
        "clinic-webhook",
        [
            AvailabilitySlotInput(
                source_ref=source_ref,
                source_provider="cliniko",
                business_id="920000001",
                clinician_id="930000001",
                appointment_type_id="940000001",
                start_at=start_at,
                end_at=start_at + timedelta(minutes=30),
                fetched_at=observed_at,
                expires_at=observed_at + timedelta(minutes=10),
            )
        ],
        now=observed_at,
    )


def _post_twilio_sms(
    client: TestClient,
    *,
    message_sid: str,
    from_address: str,
    body: str,
):
    return client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": message_sid,
            "From": from_address,
            "To": "+447700900000",
            "Body": body,
            "SmsStatus": "received",
        },
    )


def _seed(session: Session, *, include_job: bool = True) -> None:
    clinic_id = "clinic-webhook"
    session.add(
        Clinic(
            id=clinic_id,
            name="Webhook Clinic",
            sms_number="+447700900000",
            timezone="Europe/London",
            daily_caps=200,
        )
    )
    session.add(
        Patient(
            id="patient-webhook",
            clinic_id=clinic_id,
            source_ref="P-WEBHOOK",
            name="Dara Patient",
            phone="+447700900001",
            email="dara@example.test",
            consent_flags={"sms": True},
            opt_out_flags={},
        )
    )
    session.add(
        Appointment(
            id="appointment-webhook",
            clinic_id=clinic_id,
            patient_id="patient-webhook",
            source_ref="A-WEBHOOK",
            status="missed",
            start_at=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
        )
    )
    session.add(
        Campaign(
            id="campaign-webhook",
            clinic_id=clinic_id,
            type=CampaignType.RECOVERY,
            status=CampaignStatus.ACTIVE,
        )
    )
    if include_job:
        session.add(
            OutreachJob(
                id="job-webhook-sms",
                clinic_id=clinic_id,
                campaign_id="campaign-webhook",
                patient_id="patient-webhook",
                appointment_id="appointment-webhook",
                channel=Channel.SMS,
                state=OutreachState.SENT,
            )
        )


def test_eventgrid_sms_webhook_routes_opt_out_to_clinic_recall(monkeypatch):
    factory = _session_factory()
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/events",
        json=[
            {
                "id": "event-opt-out",
                "eventType": "Microsoft.Communication.SMSReceived",
                "eventTime": "2026-06-26T12:00:00Z",
                "data": {
                    "from": "+447700900001",
                    "to": "+447700900000",
                    "message": "STOP",
                },
            }
        ],
    )

    assert response.status_code == 200
    assert response.json()["routed_events"] == 1
    with factory() as session:
        patient = session.get(Patient, "patient-webhook")
        job = session.get(OutreachJob, "job-webhook-sms")
        assert patient.opt_out_flags["sms"] is True
        assert job.state == OutreachState.COMPLETED


def test_twilio_sms_for_frozen_subject_routes_anonymously_without_reassociation(
    monkeypatch,
):
    factory = _session_factory()
    with factory.begin() as session:
        session.add(
            InboundMessage(
                id="inbound-msg-before-freeze",
                clinic_id="clinic-webhook",
                provider=ClinicPhoneProvider.TWILIO,
                provider_message_id="SM-before-freeze",
                to_number="+447700900000",
                from_number_hash=inbound_messages.hash_phone_number_for_clinic(
                    "+447700900001",
                    "clinic-webhook",
                ),
                body_length=11,
                body_sha256="b" * 64,
                intent=inbound_messages.BOOKING_INTAKE_INTENT,
                payload={
                    "sms_booking_intake": {
                        "stage": inbound_messages.BOOKING_INTAKE_AWAITING_PREFERENCE,
                        "patient_id": "patient-webhook",
                        "appointment_id": "appointment-webhook",
                        "booking_kind": "change_existing",
                        "attempt_count": 0,
                    }
                },
            )
        )
        session.flush()
        request_patient_erasure(
            session,
            clinic_id="clinic-webhook",
            patient_id="patient-webhook",
            confirm_token="ERASE patient-webhook",
            request_identity="tests-webhook-freeze",
            actor_role="dpo",
            actor_reference="tests-webhook-operator",
            keyring=SubjectKeyring(
                current=SubjectKey("tests-webhook-v1", b"tests-webhook-freeze-key")
            ),
            policy=RightsPolicy(
                "tests-webhook-policy-v1",
                "a" * 64,
                timedelta(days=28),
            ),
            now=datetime(2026, 6, 26, 12, 0, tzinfo=UTC),
        )

    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    response = _post_twilio_sms(
        TestClient(_sms_test_app()),
        message_sid="SM-frozen-urgent",
        from_address="+447700900001",
        body="urgent chest pain",
    )

    assert response.status_code == 200
    with factory() as session:
        identity = resolve_single_inbound_patient_id(
            session,
            "clinic-webhook",
            inbound_messages.hash_phone_number_for_clinic(
                "+447700900001",
                "clinic-webhook",
            ),
        )
        message = session.execute(
            select(InboundMessage).where(
                InboundMessage.provider_message_id == "SM-frozen-urgent"
            )
        ).scalar_one()
        task = session.execute(select(InboundStaffTask)).scalar_one()
        patient = session.get(Patient, "patient-webhook")
        with inbound_messages.clinic_scope(session, "clinic-webhook"):
            context = inbound_messages._sms_conversation_context(
                session,
                route=sms.resolve_inbound_sms_route(
                    session,
                    provider=ClinicPhoneProvider.TWILIO,
                    inbound_number="+447700900000",
                ),
                from_address="+447700900001",
                current_message_id=message.id,
            )
        assert identity.status == "no_match"
        assert context.patient_id is None
        assert context.appointment_id is None
        assert message.intent == "urgent"
        assert "patient-webhook" not in repr(message.payload)
        assert "appointment-webhook" not in repr(message.payload)
        assert task.patient_id is None
        assert task.kind == InboundStaffTaskKind.ESCALATION
        assert patient is not None and patient.opt_out_flags == {}


def test_twilio_sms_webhook_returns_safe_reply_when_data_plane_unavailable(monkeypatch):
    def raise_no_data_plane():
        raise RuntimeError("not configured")

    monkeypatch.setattr(sms, "get_sessionmaker", raise_no_data_plane)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = _post_twilio_sms(
        client,
        message_sid="SM-no-db-safe-reply",
        from_address="+447700900001",
        body="book me",
    )

    assert response.status_code == 200
    assert "A member of the clinic team will follow up" in response.text


def test_twilio_sms_webhook_returns_safe_reply_when_number_is_unowned(monkeypatch):
    factory = _session_factory(include_job=False)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-unowned-number-safe-reply",
            "From": "+447700900001",
            "To": "+447700999999",
            "Body": "book me",
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    assert "A member of the clinic team will follow up" in response.text


def test_twilio_sms_webhook_routes_clinical_reply_to_escalation(monkeypatch):
    factory = _session_factory()
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-clinical",
            "From": "+447700900001",
            "To": "+447700900000",
            "Body": "My knee hurts",
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "I have flagged this for the clinic team" in response.text
    with factory() as session:
        escalation = session.execute(select(Escalation)).scalar_one()
        job = session.get(OutreachJob, "job-webhook-sms")
        assert escalation.reason == EscalationReason.CLINICAL
        assert job.state == OutreachState.ESCALATED


def test_twilio_sms_webhook_escalates_symptom_and_booking_reply_before_booking(monkeypatch):
    factory = _session_factory()
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-clinical-booking-rashes",
            "From": "+447700900001",
            "To": "+447700900000",
            "Body": "i want to book and appointment, i have cugh and rashes",
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    assert "I have flagged this for the clinic team" in response.text
    assert "appointment times are available" not in response.text
    with factory() as session:
        escalation = session.execute(select(Escalation)).scalar_one()
        bookings = session.execute(select(BookingAction)).scalars().all()
        job = session.get(OutreachJob, "job-webhook-sms")
        assert escalation.reason == EscalationReason.CLINICAL
        assert bookings == []
        assert job.state == OutreachState.ESCALATED


@pytest.mark.parametrize("body", ["need an appointment asap", "book mr an appointment"])
def test_twilio_sms_webhook_routes_active_rebook_reply_to_natural_booking_intake(monkeypatch, body: str):
    factory = _session_factory()
    with factory() as session:
        slot_start = datetime.now(UTC) + timedelta(days=1)
        _upsert_fresh_slot(session, "active-rebook-natural", slot_start)
        session.commit()
    _enable_synthetic_sms_t2(monkeypatch, factory, f"active-{body.replace(' ', '-')}")

    def fake_interpret(*, body: str, context_summary: dict[str, object], offered_slots: tuple[dict[str, str], ...]):
        return InboundTextIntent(
            intent="booking",
            safety="safe",
            booking_kind="new",
            time_preference="asap",
            selected_slot_ref=None,
            callback_requested=False,
            confidence=0.94,
        )

    message_sid = f"SM-active-rebook-{body.replace(' ', '-')}"
    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", fake_interpret)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": message_sid,
            "From": "+447700900001",
            "To": "+447700900000",
            "Body": body,
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    assert "These appointment times are available" in response.text
    assert "<Message>" in response.text
    with factory() as session:
        job = session.get(OutreachJob, "job-webhook-sms")
        message = session.execute(select(InboundMessage).where(InboundMessage.provider_message_id == message_sid)).scalar_one()
        assert job.state == OutreachState.REPLIED
        assert message.intent == "booking_intake"
        assert message.payload["sms_booking_intake"]["stage"] == "awaiting_slot_selection"


def test_twilio_sms_webhook_uses_text_adapter_for_active_unclear_booking_phrase(monkeypatch):
    factory = _session_factory()
    with factory() as session:
        slot_start = datetime.now(UTC) + timedelta(days=1)
        _upsert_fresh_slot(session, "active-unclear-interview", slot_start)
        session.commit()
    _enable_synthetic_sms_t2(monkeypatch, factory, "active-unclear")

    def fake_interpret(*, body: str, context_summary: dict[str, object], offered_slots: tuple[dict[str, str], ...]):
        return InboundTextIntent(
            intent="booking",
            safety="safe",
            booking_kind="new",
            time_preference="asap",
            selected_slot_ref=None,
            callback_requested=False,
            confidence=0.94,
        )

    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", fake_interpret)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-active-unclear-interview",
            "From": "+447700900001",
            "To": "+447700900000",
            "Body": "visit pls",
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    assert "These appointment times are available" in response.text
    with factory() as session:
        job = session.get(OutreachJob, "job-webhook-sms")
        message = session.execute(
            select(InboundMessage).where(InboundMessage.provider_message_id == "SM-active-unclear-interview")
        ).scalar_one()
        assert job.state == OutreachState.SENT
        assert message.intent == "booking_intake"
        assert message.payload["sms_booking_intake"]["stage"] == "awaiting_slot_selection"


def test_twilio_sms_webhook_resolves_active_clinic_phone_number(monkeypatch):
    factory = _session_factory()
    with factory() as session:
        session.add(
            ClinicPhoneNumber(
                id="clinic-phone-webhook-twilio",
                clinic_id="clinic-webhook",
                phone_number="+447700900010",
                provider=ClinicPhoneProvider.TWILIO,
                purpose=ClinicPhonePurpose.INBOUND,
                status=ClinicPhoneStatus.ACTIVE,
            )
        )
        session.commit()
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-route-table",
            "From": "+447700900001",
            "To": "+44 7700 900010",
            "Body": "My knee hurts",
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    with factory() as session:
        escalation = session.execute(select(Escalation)).scalar_one()
        assert escalation.reason == EscalationReason.CLINICAL


def test_twilio_sms_webhook_creates_callback_task_for_cold_inbound_sms(monkeypatch):
    factory = _session_factory(include_job=False)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-callback",
            "From": "+447700900099",
            "To": "+447700900000",
            "Body": "Can reception call me?",
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    assert "A member of the clinic team will call you back" in response.text
    with factory() as session:
        message = session.execute(select(InboundMessage)).scalar_one()
        task = session.execute(select(InboundStaffTask)).scalar_one()
        assert message.body_length == len("Can reception call me?")
        assert message.body_sha256.startswith("sha256:")
        assert message.summary == "Callback request from inbound SMS"
        assert task.kind == InboundStaffTaskKind.CALLBACK
        assert task.inbound_call_id is None
        assert task.inbound_message_id == message.id


def test_twilio_sms_webhook_cold_booking_request_hands_off_at_t0(monkeypatch):
    factory = _session_factory(include_job=False)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-booking",
            "From": "+447700900099",
            "To": "+447700900000",
            "Body": "I need to book",
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    assert "can't verify identity by text" in response.text
    with factory() as session:
        message = session.execute(select(InboundMessage)).scalar_one()
        tasks = session.execute(select(InboundStaffTask)).scalars().all()
        assert message.intent == "identity_unclear"
        assert message.payload["sms_conversation"] == {
            "state": "action_triggered",
            "previous_chitchat_turns": 0,
        }
        assert message.payload["sms_booking_intake"] == {
            "stage": "ready_for_staff_request",
            "attempt_count": 0,
            "prior_message_ids": [],
        }
        assert len(tasks) == 1
        assert tasks[0].kind == InboundStaffTaskKind.IDENTITY_UNCLEAR


def test_twilio_sms_webhook_falls_back_to_staff_for_unknown_sms_booking_sender(monkeypatch):
    factory = _session_factory(include_job=False)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    first = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-booking-stage-1",
            "From": "+447700900099",
            "To": "+447700900000",
            "Body": "I need to book",
            "SmsStatus": "received",
        },
    )
    assert first.status_code == 200
    assert "can't verify identity by text" in first.text
    with factory() as session:
        message = session.execute(select(InboundMessage)).scalar_one()
        task = session.execute(select(InboundStaffTask)).scalar_one()
        assert message.intent == "identity_unclear"
        assert message.payload["sms_booking_intake"]["stage"] == "ready_for_staff_request"
        assert task.kind == InboundStaffTaskKind.IDENTITY_UNCLEAR
        assert task.reason == "identity_policy_unavailable"
        assert task.payload["fallback_reason"] == "identity_policy_unavailable"


def test_twilio_sms_phone_hash_only_hands_off_without_slots_or_booking(monkeypatch):
    factory = _session_factory(include_job=False)
    with factory() as session:
        slot_start = datetime.now(UTC) + timedelta(days=1)
        _upsert_fresh_slot(session, "sms-slot-1", slot_start)
        session.commit()
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    first = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-auto-book-1",
            "From": "+447700900001",
            "To": "+447700900000",
            "Body": "I need to book",
            "SmsStatus": "received",
        },
    )
    assert first.status_code == 200
    assert "can't verify identity by text" in first.text
    assert "appointment times are available" not in first.text
    with factory() as session:
        task = session.execute(select(InboundStaffTask)).scalar_one()
        action_count = len(session.execute(select(BookingAction)).scalars().all())
        final_message = session.execute(
            select(InboundMessage).where(InboundMessage.provider_message_id == "SM-auto-book-1")
        ).scalar_one()

        assert action_count == 0
        assert task.kind == InboundStaffTaskKind.IDENTITY_UNCLEAR
        assert task.reason == "identity_policy_unavailable"
        serialized = f"{task.payload!r} {final_message.payload!r}"
        for forbidden in (
            "patient_id",
            "appointment_id",
            "selected_slot_id",
            "booking_kind",
            "preference_bucket",
            "slot_offer",
        ):
            assert forbidden not in serialized
        assert final_message.intent == "identity_unclear"
        assert final_message.payload["sms_booking_intake"]["stage"] == "ready_for_staff_request"


def test_twilio_sms_webhook_escalates_urgent_cold_inbound_sms(monkeypatch):
    factory = _session_factory(include_job=False)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-urgent",
            "From": "+447700900099",
            "To": "+447700900000",
            "Body": "I have chest pain",
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    assert "I have flagged this for the clinic team" in response.text
    with factory() as session:
        task = session.execute(select(InboundStaffTask)).scalar_one()
        assert task.kind == InboundStaffTaskKind.ESCALATION
        assert task.priority == "high"
        assert task.reason == "urgent"


def test_twilio_sms_webhook_safeguarding_precedes_booking_and_creates_one_task(monkeypatch):
    factory = _session_factory(include_job=False)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-safeguarding-booking",
            "From": "+447700900099",
            "To": "+447700900000",
            "Body": "I feel unsafe at home and need to book an appointment",
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    assert "I have flagged this for the clinic team" in response.text
    with factory() as session:
        tasks = session.execute(select(InboundStaffTask)).scalars().all()
        assert len(tasks) == 1
        escalation = tasks[0]
        assert escalation.kind == InboundStaffTaskKind.ESCALATION
        assert escalation.reason == "safeguarding"
        assert escalation.priority == "high"


def test_twilio_sms_webhook_handles_greeting_as_chitchat_without_staff_task(monkeypatch):
    factory = _session_factory(include_job=False)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-hi",
            "From": "+447700900099",
            "To": "+447700900000",
            "Body": "hi",
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    assert "How can I help with appointments or clinic information" in response.text
    with factory() as session:
        message = session.execute(select(InboundMessage)).scalar_one()
        tasks = session.execute(select(InboundStaffTask)).scalars().all()
        assert message.intent == "chitchat"
        assert message.summary == "Conversational inbound SMS acknowledged without staff routing"
        assert tasks == []


def test_twilio_sms_webhook_handles_greeting_variants_as_chitchat(monkeypatch):
    factory = _session_factory(include_job=False)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    for index, body in enumerate(["hiya", "heya", "hiii", "hey"], start=1):
        response = client.post(
            "/api/v1/sms/twilio",
            data={
                "AccountSid": "AC-test",
                "MessageSid": f"SM-greeting-variant-{index}",
                "From": f"+44770090009{index}",
                "To": "+447700900000",
                "Body": body,
                "SmsStatus": "received",
            },
        )

        assert response.status_code == 200
        assert "How can I help with appointments or clinic information" in response.text

    with factory() as session:
        messages = session.execute(select(InboundMessage)).scalars().all()
        tasks = session.execute(select(InboundStaffTask)).scalars().all()
        assert {message.intent for message in messages} == {"chitchat"}
        assert tasks == []


def test_twilio_sms_webhook_continues_chitchat_until_action_intent(monkeypatch):
    factory = _session_factory(include_job=False)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    first = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-chitchat-1",
            "From": "+447700900099",
            "To": "+447700900000",
            "Body": "hi",
            "SmsStatus": "received",
        },
    )
    second = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-chitchat-2",
            "From": "+447700900099",
            "To": "+447700900000",
            "Body": "how are you",
            "SmsStatus": "received",
        },
    )
    third = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-book-after-chat",
            "From": "+447700900099",
            "To": "+447700900000",
            "Body": "I need to book",
            "SmsStatus": "received",
        },
    )

    assert first.status_code == 200
    assert "How can I help with appointments or clinic information" in first.text
    assert second.status_code == 200
    assert "ready to help" in second.text
    assert third.status_code == 200
    assert "can't verify identity by text" in third.text
    with factory() as session:
        messages = session.execute(select(InboundMessage)).scalars().all()
        chitchat_messages = [message for message in messages if message.intent == "chitchat"]
        booking_message = next(message for message in messages if message.provider_message_id == "SM-book-after-chat")
        tasks = session.execute(select(InboundStaffTask)).scalars().all()

        assert len(chitchat_messages) == 2
        second_message = next(message for message in chitchat_messages if message.provider_message_id == "SM-chitchat-2")
        assert second_message.payload["sms_conversation"] == {
            "state": "awaiting_intent",
            "previous_chitchat_turns": 1,
            "turn": 2,
            "reason": "wellbeing_question",
        }
        assert booking_message.intent == "identity_unclear"
        assert booking_message.payload["sms_conversation"] == {
            "state": "action_triggered",
            "previous_chitchat_turns": 2,
        }
        assert booking_message.payload["sms_booking_intake"] == {
            "stage": "ready_for_staff_request",
            "attempt_count": 0,
            "prior_message_ids": [],
        }
        assert len(tasks) == 1
        assert tasks[0].kind == InboundStaffTaskKind.IDENTITY_UNCLEAR


def test_twilio_sms_webhook_does_not_guess_patient_for_unknown_stop(monkeypatch):
    factory = _session_factory(include_job=False)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = client.post(
        "/api/v1/sms/twilio",
        data={
            "AccountSid": "AC-test",
            "MessageSid": "SM-stop-unknown",
            "From": "+447700900099",
            "To": "+447700900000",
            "Body": "STOP",
            "SmsStatus": "received",
        },
    )

    assert response.status_code == 200
    assert "<Message>" not in response.text
    with factory() as session:
        patient = session.get(Patient, "patient-webhook")
        task = session.execute(select(InboundStaffTask)).scalar_one()
        assert patient.opt_out_flags == {}
        assert task.kind == InboundStaffTaskKind.IDENTITY_UNCLEAR
        assert task.patient_id is None


@pytest.mark.parametrize(
    ("body", "booking_kind", "time_preference", "expected_bucket"),
    [
        ("please book me for next Tuesday morning", "new", "next_tuesday_morning", "morning"),
        ("book me for the earliest slot", "new", "earliest", "next_available"),
        ("can you move my appointment to Friday?", "change_existing", "friday", "this_week"),
    ],
)
def test_twilio_sms_webhook_uses_text_agent_to_offer_slots_for_natural_booking_requests(
    monkeypatch,
    body: str,
    booking_kind: str,
    time_preference: str,
    expected_bucket: str,
):
    factory = _session_factory(include_job=False)
    with factory() as session:
        slot_start = datetime.now(UTC) + timedelta(days=1)
        _upsert_fresh_slot(session, f"natural-{expected_bucket}", slot_start)
        session.commit()
    _enable_synthetic_sms_t2(monkeypatch, factory, f"natural-{expected_bucket}")
    calls: list[dict[str, object]] = []

    def fake_interpret(*, body: str, context_summary: dict[str, object], offered_slots: tuple[dict[str, str], ...]):
        calls.append({"body": body, "context_summary": context_summary, "offered_slots": offered_slots})
        return InboundTextIntent(
            intent="booking",
            safety="safe",
            booking_kind=booking_kind,
            time_preference=time_preference,
            selected_slot_ref=None,
            callback_requested=False,
            confidence=0.94,
        )

    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", fake_interpret)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = _post_twilio_sms(
        client,
        message_sid=f"SM-natural-{expected_bucket}",
        from_address="+447700900001",
        body=body,
    )

    assert response.status_code == 200
    assert "These appointment times are available" in response.text
    assert "Which one works best" in response.text
    assert "Reply 1" not in response.text
    assert len(calls) == 1
    assert calls[0]["context_summary"] == {"offered_slot_count": 0}
    with factory() as session:
        message = session.execute(select(InboundMessage)).scalar_one()
        tasks = session.execute(select(InboundStaffTask)).scalars().all()
        assert message.intent == "booking_intake"
        assert message.payload["sms_booking_intake"]["stage"] == "awaiting_slot_selection"
        assert message.payload["sms_booking_intake"]["booking_kind"] == booking_kind
        assert message.payload["sms_booking_intake"]["preference_bucket"] == expected_bucket
        assert body not in str(message.payload)
        assert tasks == []


@pytest.mark.parametrize("selection_body", ["the first one", "10am works", "that one"])
def test_twilio_sms_webhook_books_known_patient_from_natural_slot_selection(monkeypatch, selection_body: str):
    factory = _session_factory(include_job=False)
    with factory() as session:
        slot_start = (datetime.now(UTC) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        summaries = _upsert_fresh_slot(
            session,
            f"natural-select-{selection_body}",
            slot_start,
        )
        slot_id = summaries[0].slot_id
        session.commit()
    _enable_synthetic_sms_t2(
        monkeypatch,
        factory,
        f"selection-{selection_body.replace(' ', '-')}",
    )

    def fake_interpret(*, body: str, context_summary: dict[str, object], offered_slots: tuple[dict[str, str], ...]):
        if offered_slots:
            return InboundTextIntent(
                intent="booking",
                safety="safe",
                booking_kind="new",
                time_preference=None,
                selected_slot_ref=None,
                callback_requested=False,
                confidence=0.93,
            )
        return InboundTextIntent(
            intent="booking",
            safety="safe",
            booking_kind="new",
            time_preference="earliest",
            selected_slot_ref=None,
            callback_requested=False,
            confidence=0.93,
        )

    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", fake_interpret)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    offer = _post_twilio_sms(
        client,
        message_sid=f"SM-natural-offer-{selection_body}",
        from_address="+447700900001",
        body="book me for the earliest slot",
    )
    booked = _post_twilio_sms(
        client,
        message_sid=f"SM-natural-select-{selection_body}",
        from_address="+447700900001",
        body=selection_body,
    )

    assert offer.status_code == 200
    assert "Which one works best" in offer.text
    assert booked.status_code == 200
    assert "not yet confirmed" in booked.text
    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        final_message = session.execute(
            select(InboundMessage).where(InboundMessage.provider_message_id == f"SM-natural-select-{selection_body}")
        ).scalar_one()
        tasks = session.execute(select(InboundStaffTask)).scalars().all()
        assert action.status == BookingActionStatus.PENDING
        assert action.availability_slot_id == slot_id
        assert final_message.intent == "booking_pending"
        assert len(tasks) == 1
        assert tasks[0].kind == InboundStaffTaskKind.BOOKING_REQUEST


def test_twilio_sms_webhook_never_calls_text_agent_for_clinical_booking_sms(monkeypatch):
    factory = _session_factory(include_job=False)

    def fail_if_called(**_kwargs):
        raise AssertionError("text agent should not run before the clinical safety gate")

    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", fail_if_called)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = _post_twilio_sms(
        client,
        message_sid="SM-clinical-before-agent",
        from_address="+447700900099",
        body="I have chest pain and need to book an appointment",
    )

    assert response.status_code == 200
    assert "I have flagged this for the clinic team" in response.text
    with factory() as session:
        tasks = session.execute(select(InboundStaffTask)).scalars().all()
        assert len(tasks) == 1
        assert tasks[0].kind == InboundStaffTaskKind.ESCALATION


def test_twilio_sms_webhook_falls_back_when_text_agent_raises(monkeypatch):
    factory = _session_factory(include_job=False)
    _enable_synthetic_sms_t2(monkeypatch, factory, "agent-failure")

    def raise_from_text_agent(**_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", raise_from_text_agent)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = _post_twilio_sms(
        client,
        message_sid="SM-agent-raises-fallback",
        from_address="+447700900001",
        body="I need to book",
    )

    assert response.status_code == 200
    assert "new appointment, changing an existing one" in response.text
    with factory() as session:
        message = session.execute(select(InboundMessage)).scalar_one()
        assert message.intent == "booking_intake"
        assert message.payload["sms_booking_intake"]["stage"] == "awaiting_booking_kind"


def test_twilio_sms_webhook_no_availability_never_false_confirms(monkeypatch):
    factory = _session_factory(include_job=False)
    _enable_synthetic_sms_t2(monkeypatch, factory, "no-availability")

    def fake_interpret(*, body: str, context_summary: dict[str, object], offered_slots: tuple[dict[str, str], ...]):
        return InboundTextIntent(
            intent="booking",
            safety="safe",
            booking_kind="new",
            time_preference="earliest",
            selected_slot_ref=None,
            callback_requested=False,
            confidence=0.95,
        )

    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", fake_interpret)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = _post_twilio_sms(
        client,
        message_sid="SM-no-availability",
        from_address="+447700900001",
        body="book me for the earliest slot",
    )

    assert response.status_code == 200
    assert "couldn't safely confirm" in response.text
    assert "You're booked" not in response.text
    assert "<Message>" in response.text
    with factory() as session:
        task = session.execute(select(InboundStaffTask)).scalar_one()
        bookings = session.execute(select(BookingAction)).scalars().all()
        message = session.execute(select(InboundMessage)).scalar_one()
        assert task.kind == InboundStaffTaskKind.BOOKING_REQUEST
        assert task.reason == "no_availability"
        assert bookings == []
        assert message.intent == "booking_request"


def test_twilio_sms_webhook_provider_error_never_false_confirms(monkeypatch):
    factory = _session_factory(include_job=False)
    _enable_synthetic_sms_t2(monkeypatch, factory, "provider-error")

    def fake_interpret(*, body: str, context_summary: dict[str, object], offered_slots: tuple[dict[str, str], ...]):
        return InboundTextIntent(
            intent="booking",
            safety="safe",
            booking_kind="new",
            time_preference="earliest",
            selected_slot_ref=None,
            callback_requested=False,
            confidence=0.95,
        )

    def raise_availability(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", fake_interpret)
    monkeypatch.setattr(inbound_messages, "get_availability", raise_availability)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = _post_twilio_sms(
        client,
        message_sid="SM-provider-error",
        from_address="+447700900001",
        body="book me for the earliest slot",
    )

    assert response.status_code == 200
    assert "couldn't safely confirm" in response.text
    assert "You're booked" not in response.text
    with factory() as session:
        task = session.execute(select(InboundStaffTask)).scalar_one()
        bookings = session.execute(select(BookingAction)).scalars().all()
        assert task.reason == "availability_lookup_failed"
        assert bookings == []


def test_twilio_sms_webhook_multiple_identity_handoff_never_reads_phi(monkeypatch):
    factory = _session_factory(include_job=False)
    with factory() as session:
        session.add(
            Patient(
                id="patient-webhook-shared",
                clinic_id="clinic-webhook",
                source_ref="P-WEBHOOK-SHARED",
                name="Shared Patient",
                phone="+447700900001",
                email="shared@example.test",
                consent_flags={"sms": True},
                opt_out_flags={},
            )
        )
        slot_start = datetime.now(UTC) + timedelta(days=1)
        _upsert_fresh_slot(session, "multiple-identity-slot", slot_start)
        session.commit()

    def fake_interpret(**_kwargs):
        raise AssertionError("shared-number T0 handoff must precede the text model")

    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", fake_interpret)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = _post_twilio_sms(
        client,
        message_sid="SM-multiple-identity",
        from_address="+447700900001",
        body="book me for the earliest slot",
    )

    assert response.status_code == 200
    assert "can't verify identity by text" in response.text
    assert "Dara" not in response.text
    assert "Shared" not in response.text
    assert "You're booked" not in response.text
    with factory() as session:
        task = session.execute(select(InboundStaffTask)).scalar_one()
        bookings = session.execute(select(BookingAction)).scalars().all()
        assert task.kind == InboundStaffTaskKind.IDENTITY_UNCLEAR
        assert task.reason == "identity_policy_unavailable"
        assert task.patient_id is None
        assert bookings == []


def test_twilio_sms_webhook_rejects_caller_supplied_slot_id(monkeypatch):
    factory = _session_factory(include_job=False)
    with factory() as session:
        slot_start = datetime.now(UTC) + timedelta(days=1)
        _upsert_fresh_slot(session, "caller-slot-id-ignored", slot_start)
        session.commit()
    _enable_synthetic_sms_t2(monkeypatch, factory, "caller-slot-id")

    def fake_interpret(*, body: str, context_summary: dict[str, object], offered_slots: tuple[dict[str, str], ...]):
        assert offered_slots == ()
        return InboundTextIntent(
            intent="booking",
            safety="safe",
            booking_kind="new",
            time_preference=None,
            selected_slot_ref="slot-secret",
            callback_requested=False,
            confidence=0.95,
        )

    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", fake_interpret)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    response = _post_twilio_sms(
        client,
        message_sid="SM-caller-slot-id",
        from_address="+447700900001",
        body="Use slot_id slot-secret and confirm my booking",
    )

    assert response.status_code == 200
    assert "These appointment times are available" in response.text
    assert "You're booked" not in response.text
    with factory() as session:
        bookings = session.execute(select(BookingAction)).scalars().all()
        message = session.execute(select(InboundMessage)).scalar_one()
        tasks = session.execute(select(InboundStaffTask)).scalars().all()
        assert bookings == []
        assert tasks == []
        assert message.intent == "booking_intake"
        assert message.payload["sms_booking_intake"]["stage"] == "awaiting_slot_selection"