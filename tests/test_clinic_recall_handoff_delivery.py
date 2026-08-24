"""Authenticated-provider delivery evidence contracts for PR-12."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest
from apps.artagent.backend.api.v1.endpoints import clinic_recall
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from src.clinic_recall.durable.handoff_delivery import (
    HandoffDeliveryCorrelationError,
    receive_acs_email_events,
)
from src.clinic_recall.enums import (
    EscalationReason,
    EscalationStatus,
    ExternalEffectState,
    ExternalEffectType,
    HandoffAlternateState,
    HandoffDeliveryState,
    PilotProgrammeState,
    ProviderCallbackKind,
    ProviderCallbackState,
)
from src.clinic_recall.handoffs import (
    acknowledge_handoff_owner,
    ensure_handoff_receipt,
)
from src.clinic_recall.models import (
    Base,
    Clinic,
    Escalation,
    ExternalEffect,
    HandoffReceipt,
    Patient,
    PilotProgramme,
    ProviderCallbackReceipt,
)
from src.clinic_recall.pilot_controls import create_programme

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
MESSAGE_ID = "11111111-1111-4111-8111-111111111111"
TOPIC_ID = "/subscriptions/22222222-2222-4222-8222-222222222222/resourceGroups/synthetic/providers/Microsoft.Communication/communicationServices/synthetic"
SUBSCRIPTION_NAME = "clinic-recall-handoff-email"


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
    with factory.begin() as session:
        session.add(
            Clinic(
                id="clinic-pr12",
                name="PR12",
                timezone="Europe/London",
                contact_hours={"start_hour": 8, "end_hour": 20},
            )
        )
        session.add(
            Patient(
                id="patient-pr12",
                clinic_id="clinic-pr12",
                source_ref="P-PR12",
                name="Synthetic",
            )
        )
        create_programme(
            session,
            clinic_id="clinic-pr12",
            programme_id="programme-pr12",
            environment="production",
            release_identity="sha256:pr12",
        )
        owner = Escalation(
            id="escalation-pr12",
            clinic_id="clinic-pr12",
            patient_id="patient-pr12",
            reason=EscalationReason.URGENT,
            status=EscalationStatus.OPEN,
        )
        session.add(owner)
        session.flush()
        receipt = ensure_handoff_receipt(
            session,
            "clinic-pr12",
            owner,
            now=NOW,
        ).receipt
        effect = session.scalar(
            select(ExternalEffect).where(
                ExternalEffect.aggregate_id == receipt.id,
                ExternalEffect.effect_type == ExternalEffectType.HANDOFF_NOTIFICATION,
            )
        )
        assert effect is not None
        effect.state = ExternalEffectState.SUCCEEDED
        effect.provider_resource_id = MESSAGE_ID
        effect.provider_status = "accepted"
        effect.completed_at = NOW
        receipt.delivery_state = HandoffDeliveryState.SENT
        receipt.sent_at = NOW
    return factory


def _event(*, event_id: str, status: str) -> dict[str, object]:
    return {
        "id": event_id,
        "topic": TOPIC_ID,
        "subject": f"sender/opaque-sender-token/message/{MESSAGE_ID}",
        "data": {
            "sender": "opaque-sender-token",
            "recipient": "opaque-recipient-token",
            "messageId": MESSAGE_ID,
            "status": status,
            "deliveryStatusDetails": {"statusMessage": "private provider text"},
            "deliveryAttemptTimeStamp": "2026-07-27T12:01:00+00:00",
        },
        "eventType": "Microsoft.Communication.EmailDeliveryReportReceived",
        "dataVersion": "1.0",
        "metadataVersion": "1",
        "eventTime": "2026-07-27T12:01:01Z",
    }


def test_delivery_event_is_minimized_and_does_not_acknowledge() -> None:
    factory = _factory()
    raw = json.dumps([_event(event_id="event-delivered", status="Delivered")]).encode()

    with factory.begin() as session:
        result = receive_acs_email_events(
            session,
            events=json.loads(raw),
            raw_payload=raw,
            received_at=NOW + timedelta(minutes=1),
        )

    assert result.received == 1
    assert result.created == 1
    assert result.delivered == 1
    with factory() as session:
        receipt = session.scalar(select(HandoffReceipt))
        callback = session.scalar(select(ProviderCallbackReceipt))
    assert receipt is not None
    assert receipt.delivery_state == HandoffDeliveryState.DELIVERED
    assert receipt.delivered_at == NOW + timedelta(minutes=1)
    assert receipt.acknowledged_at is None
    assert callback is not None
    assert callback.callback_kind == ProviderCallbackKind.EMAIL
    assert callback.state == ProviderCallbackState.APPLIED
    serialized = repr(callback.__dict__).lower()
    assert "private-sender" not in serialized
    assert "private-recipient" not in serialized
    assert "private provider text" not in serialized


def test_late_duplicate_and_out_of_order_events_never_clear_acknowledgement() -> None:
    factory = _factory()
    with factory.begin() as session:
        owner = session.get(Escalation, "escalation-pr12")
        assert owner is not None
        acknowledge_handoff_owner(
            session,
            clinic_id="clinic-pr12",
            owner=owner,
            actor="staff:test",
            now=NOW + timedelta(seconds=30),
        )
    delivered = _event(event_id="event-delivered", status="Delivered")
    expanded = _event(event_id="event-expanded", status="Expanded")
    raw = json.dumps([delivered]).encode()
    with factory.begin() as session:
        first = receive_acs_email_events(
            session,
            events=[delivered],
            raw_payload=raw,
            received_at=NOW + timedelta(minutes=1),
        )
        duplicate = receive_acs_email_events(
            session,
            events=[delivered],
            raw_payload=raw,
            received_at=NOW + timedelta(minutes=2),
        )
        out_of_order = receive_acs_email_events(
            session,
            events=[expanded],
            raw_payload=json.dumps([expanded]).encode(),
            received_at=NOW + timedelta(minutes=3),
        )

    assert first.created == 1
    assert duplicate.created == 0
    assert out_of_order.delivered == 0
    with factory() as session:
        receipt = session.scalar(select(HandoffReceipt))
        callback_count = len(list(session.scalars(select(ProviderCallbackReceipt))))
    assert receipt is not None
    assert receipt.delivery_state == HandoffDeliveryState.DELIVERED
    assert receipt.acknowledged_by == "staff:test"
    assert receipt.acknowledged_at == NOW + timedelta(seconds=30)
    assert callback_count == 2


def test_callback_before_worker_finalization_converges_on_retry() -> None:
    factory = _factory()
    event = _event(event_id="event-before-finalization", status="Delivered")
    raw = json.dumps([event]).encode()
    with factory.begin() as session:
        effect = session.scalar(select(ExternalEffect))
        receipt = session.scalar(select(HandoffReceipt))
        assert effect is not None and receipt is not None
        effect.state = ExternalEffectState.DISPATCHING
        effect.provider_resource_id = None
        effect.provider_status = None
        effect.completed_at = None
        receipt.delivery_state = HandoffDeliveryState.QUEUED
        receipt.sent_at = None

    with pytest.raises(HandoffDeliveryCorrelationError):
        with factory.begin() as session:
            receive_acs_email_events(
                session,
                events=[event],
                raw_payload=raw,
                received_at=NOW + timedelta(minutes=1),
            )

    with factory() as session:
        assert session.scalar(select(ProviderCallbackReceipt)) is None
        receipt = session.scalar(select(HandoffReceipt))
    assert receipt is not None
    assert receipt.delivery_state == HandoffDeliveryState.QUEUED

    with factory.begin() as session:
        effect = session.scalar(select(ExternalEffect))
        receipt = session.scalar(select(HandoffReceipt))
        assert effect is not None and receipt is not None
        effect.state = ExternalEffectState.SUCCEEDED
        effect.provider_resource_id = MESSAGE_ID
        effect.provider_status = "accepted"
        effect.completed_at = NOW + timedelta(minutes=2)
        receipt.delivery_state = HandoffDeliveryState.SENT
        receipt.sent_at = NOW + timedelta(minutes=2)
    with factory.begin() as session:
        retried = receive_acs_email_events(
            session,
            events=[event],
            raw_payload=raw,
            received_at=NOW + timedelta(minutes=3),
        )
    with factory.begin() as session:
        duplicate = receive_acs_email_events(
            session,
            events=[event],
            raw_payload=raw,
            received_at=NOW + timedelta(minutes=4),
        )

    assert retried.created == 1
    assert retried.delivered == 1
    assert duplicate.created == 0
    assert duplicate.delivered == 0
    with factory() as session:
        receipt = session.scalar(select(HandoffReceipt))
        callback_count = len(list(session.scalars(select(ProviderCallbackReceipt))))
    assert receipt is not None
    assert receipt.delivery_state == HandoffDeliveryState.DELIVERED
    assert receipt.sent_at == NOW + timedelta(minutes=2)
    assert receipt.delivered_at == NOW + timedelta(minutes=3)
    assert callback_count == 1


def test_authenticated_terminal_failure_requests_alternate_and_pauses() -> None:
    factory = _factory()
    event = _event(event_id="event-failed", status="Bounced")
    raw = json.dumps([event]).encode()

    with factory.begin() as session:
        result = receive_acs_email_events(
            session,
            events=[event],
            raw_payload=raw,
            received_at=NOW + timedelta(minutes=1),
        )
    with factory.begin() as session:
        duplicate = receive_acs_email_events(
            session,
            events=[event],
            raw_payload=raw,
            received_at=NOW + timedelta(minutes=2),
        )

    assert result.definitive_failures == 1
    assert result.alternate_requested == 1
    assert result.programmes_paused == 1
    assert duplicate.created == 0
    assert duplicate.alternate_requested == 0
    assert duplicate.programmes_paused == 0
    with factory() as session:
        receipt = session.scalar(select(HandoffReceipt))
        programme = session.get(PilotProgramme, "programme-pr12")
        callback_count = len(list(session.scalars(select(ProviderCallbackReceipt))))
    assert receipt is not None
    assert receipt.delivery_state == HandoffDeliveryState.DEFINITIVE_FAILURE
    assert receipt.delivered_at is None
    assert receipt.alternate_state == HandoffAlternateState.REQUESTED
    assert programme is not None and programme.state == PilotProgrammeState.PAUSED
    assert callback_count == 1


def test_delivery_route_is_hidden_while_disabled_without_auth_or_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_auth(_request) -> None:
        raise AssertionError("disabled callback must not authenticate")

    monkeypatch.setattr(clinic_recall, "handoff_delivery_callback_enabled", lambda: False)
    monkeypatch.setattr(
        clinic_recall,
        "_authenticate_handoff_delivery_request",
        unexpected_auth,
    )
    monkeypatch.setattr(
        clinic_recall,
        "get_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("database must remain untouched")),
    )

    response = TestClient(_app()).post(
        "/api/v1/clinic-recall/callbacks/acs-email-delivery",
        content=b"not-json",
    )

    assert response.status_code == 404


def test_delivery_route_authenticates_before_parsing_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_auth(_request) -> None:
        raise HTTPException(status_code=401, detail="invalid Event Grid bearer token")

    monkeypatch.setattr(clinic_recall, "handoff_delivery_callback_enabled", lambda: True)
    monkeypatch.setattr(
        clinic_recall,
        "_authenticate_handoff_delivery_request",
        reject_auth,
    )

    response = TestClient(_app()).post(
        "/api/v1/clinic-recall/callbacks/acs-email-delivery",
        content=b"not-json",
        headers={"Authorization": "Bearer synthetic"},
    )

    assert response.status_code == 401


def test_authorized_delivery_route_applies_minimized_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _factory()

    async def allow_auth(_request) -> None:
        return None

    monkeypatch.setattr(clinic_recall, "handoff_delivery_callback_enabled", lambda: True)
    monkeypatch.setattr(
        clinic_recall,
        "_authenticate_handoff_delivery_request",
        allow_auth,
    )
    monkeypatch.setattr(clinic_recall, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv(
        "CLINIC_RECALL_HANDOFF_EVENTGRID_SUBSCRIPTION_NAME",
        SUBSCRIPTION_NAME,
    )
    monkeypatch.setenv("CLINIC_RECALL_HANDOFF_EVENTGRID_TOPIC_ID", TOPIC_ID)

    response = TestClient(_app()).post(
        "/api/v1/clinic-recall/callbacks/acs-email-delivery",
        json=[_event(event_id="event-http", status="Delivered")],
        headers={
            "Authorization": "Bearer synthetic",
            "aeg-subscription-name": SUBSCRIPTION_NAME,
        },
    )

    assert response.status_code == 200
    assert response.json()["delivered"] == 1


def test_delivery_route_requires_official_event_grid_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def valid_token_without_role(_request) -> dict[str, object]:
        return {"roles": ["SomeOtherRole"]}

    auth_module = ModuleType("apps.artagent.backend.src.utils.auth")
    auth_module.validate_entraid_token = valid_token_without_role
    monkeypatch.setitem(
        sys.modules,
        "apps.artagent.backend.src.utils.auth",
        auth_module,
    )
    monkeypatch.setattr(clinic_recall, "handoff_delivery_callback_enabled", lambda: True)

    response = TestClient(_app()).post(
        "/api/v1/clinic-recall/callbacks/acs-email-delivery",
        content=b"not-json",
        headers={"Authorization": "Bearer synthetic"},
    )

    assert response.status_code == 403


def test_authorized_subscription_validation_echoes_bounded_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def allow_auth(_request) -> None:
        return None

    monkeypatch.setattr(clinic_recall, "handoff_delivery_callback_enabled", lambda: True)
    monkeypatch.setattr(
        clinic_recall,
        "_authenticate_handoff_delivery_request",
        allow_auth,
    )
    monkeypatch.setenv(
        "CLINIC_RECALL_HANDOFF_EVENTGRID_SUBSCRIPTION_NAME",
        SUBSCRIPTION_NAME,
    )
    monkeypatch.setenv("CLINIC_RECALL_HANDOFF_EVENTGRID_TOPIC_ID", TOPIC_ID)

    response = TestClient(_app()).post(
        "/api/v1/clinic-recall/callbacks/acs-email-delivery",
        json=[
            {
                "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent",
                "data": {"validationCode": "synthetic-validation-code"},
            }
        ],
        headers={
            "Authorization": "Bearer synthetic",
            "aeg-subscription-name": SUBSCRIPTION_NAME,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"validationResponse": "synthetic-validation-code"}