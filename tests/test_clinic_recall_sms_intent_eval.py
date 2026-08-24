"""Offline evals for natural-language SMS intent handling.

These tests exercise the webhook with mocked LLM interpretation so we can
validate realistic SMS variants without sending live Twilio messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from apps.artagent.backend.api.v1.endpoints import sms
from fastapi.testclient import TestClient
from sqlalchemy import select
from src.clinic_recall import inbound_messages
from src.clinic_recall.enums import BookingActionStatus, EscalationReason, OutreachState
from src.clinic_recall.inbound_text_agent import InboundTextIntent
from src.clinic_recall.messaging.inbound import classify_intent
from src.clinic_recall.models import BookingAction, Escalation, InboundMessage, OutreachJob

from tests.test_clinic_recall_sms_webhook_integration import (
    _enable_synthetic_sms_t2,
    _post_twilio_sms,
    _session_factory,
    _sms_test_app,
    _upsert_fresh_slot,
)


@dataclass(frozen=True)
class IntentEvalCase:
    text: str
    expected_intent: str


@pytest.mark.parametrize(
    "case",
    [
        IntentEvalCase("book me an appointment", "rebook"),
        IntentEvalCase("book mr an appointment", "rebook"),
        IntentEvalCase("need an appointment asap", "rebook"),
        IntentEvalCase("book an interview", "rebook"),
        IntentEvalCase("need an interview asap", "rebook"),
        IntentEvalCase("schedule an interview", "rebook"),
        IntentEvalCase("i want to schedule a visit", "rebook"),
        IntentEvalCase("consultation asap", "rebook"),
        IntentEvalCase("can i get a visit?", "question"),
        IntentEvalCase("hello", "unclear"),
        IntentEvalCase("i want to book and appointment, i have cugh and rashes", "clinical"),
        IntentEvalCase("i have cough", "clinical"),
        IntentEvalCase("i have rashes", "clinical"),
        IntentEvalCase("stop", "opt_out"),
    ],
)
def test_sms_deterministic_safety_and_intent_boundary_eval(case: IntentEvalCase) -> None:
    assert classify_intent(case.text).value == case.expected_intent


@dataclass(frozen=True)
class WebhookEvalCase:
    text: str
    expected_reply: str
    expected_message_intent: str | None
    expected_stage: str | None = None
    include_job: bool = True
    expect_job_state: OutreachState | None = None
    expect_escalation: EscalationReason | None = None
    expect_no_booking: bool = False


def _fake_text_intent_for_eval(
    *,
    body: str,
    context_summary: dict[str, object],
    offered_slots: tuple[dict[str, str], ...],
) -> InboundTextIntent:
    text = body.lower()
    if "callback" in text or "call me" in text or "reception" in text:
        return InboundTextIntent(intent="callback", safety="safe", callback_requested=True, confidence=0.95)
    if any(term in text for term in ("book", "appointment", "interview", "visit", "consultation", "asap", "schedule")):
        return InboundTextIntent(
            intent="booking",
            safety="safe",
            booking_kind="new",
            time_preference="asap",
            selected_slot_ref=None,
            callback_requested=False,
            confidence=0.95,
        )
    return InboundTextIntent(intent="chitchat", safety="safe", confidence=0.95)


@pytest.mark.parametrize(
    "case",
    [
        WebhookEvalCase(
            text="hello",
            expected_reply="How can I help with appointments or clinic information",
            expected_message_intent="chitchat",
            include_job=False,
        ),
        WebhookEvalCase(
            text="book mr an appointment",
            expected_reply="These appointment times are available",
            expected_message_intent="booking_intake",
            expected_stage="awaiting_slot_selection",
            expect_job_state=OutreachState.REPLIED,
        ),
        WebhookEvalCase(
            text="need an appointment asap",
            expected_reply="These appointment times are available",
            expected_message_intent="booking_intake",
            expected_stage="awaiting_slot_selection",
            expect_job_state=OutreachState.REPLIED,
        ),
        WebhookEvalCase(
            text="need an interview asap",
            expected_reply="These appointment times are available",
            expected_message_intent="booking_intake",
            expected_stage="awaiting_slot_selection",
            expect_job_state=OutreachState.REPLIED,
        ),
        WebhookEvalCase(
            text="visit pls",
            expected_reply="These appointment times are available",
            expected_message_intent="booking_intake",
            expected_stage="awaiting_slot_selection",
            expect_job_state=OutreachState.SENT,
        ),
        WebhookEvalCase(
            text="can reception call me",
            expected_reply="A member of the clinic team will call you back",
            expected_message_intent="callback",
            include_job=False,
        ),
        WebhookEvalCase(
            text="i want to book and appointment, i have cugh and rashes",
            expected_reply="I have flagged this for the clinic team",
            expected_message_intent=None,
            expect_job_state=OutreachState.ESCALATED,
            expect_escalation=EscalationReason.CLINICAL,
            expect_no_booking=True,
        ),
    ],
)
def test_sms_webhook_natural_language_intent_eval(monkeypatch, case: WebhookEvalCase) -> None:
    factory = _session_factory(include_job=case.include_job)
    with factory() as session:
        slot_start = datetime.now(UTC) + timedelta(days=1)
        _upsert_fresh_slot(
            session,
            f"eval-{case.text[:12].replace(' ', '-')}",
            slot_start,
        )
        session.commit()
    if case.expected_stage is not None:
        _enable_synthetic_sms_t2(
            monkeypatch,
            factory,
            f"intent-eval-{abs(hash(case.text))}",
        )

    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", _fake_text_intent_for_eval)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())
    message_sid = f"SM-eval-{abs(hash(case.text))}"

    response = _post_twilio_sms(
        client,
        message_sid=message_sid,
        from_address="+447700900001",
        body=case.text,
    )

    assert response.status_code == 200
    assert case.expected_reply in response.text

    with factory() as session:
        message = session.execute(select(InboundMessage).where(InboundMessage.provider_message_id == message_sid)).scalar_one_or_none()
        if case.expected_message_intent is None:
            assert message is None
        else:
            assert message is not None
            assert message.intent == case.expected_message_intent
            if case.expected_stage:
                assert message.payload["sms_booking_intake"]["stage"] == case.expected_stage

        if case.expect_job_state is not None:
            job = session.get(OutreachJob, "job-webhook-sms")
            assert job.state == case.expect_job_state

        if case.expect_escalation is not None:
            escalation = session.execute(select(Escalation)).scalar_one()
            assert escalation.reason == case.expect_escalation

        if case.expect_no_booking:
            assert session.execute(select(BookingAction)).scalars().all() == []


@pytest.mark.parametrize("selection_text", ["yes please book that", "book me", "10am works", "first one"])
def test_sms_webhook_slot_selection_eval(monkeypatch, selection_text: str) -> None:
    factory = _session_factory(include_job=False)
    with factory() as session:
        slot_start = (datetime.now(UTC) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        summaries = _upsert_fresh_slot(
            session,
            f"eval-slot-{selection_text}",
            slot_start,
        )
        slot_id = summaries[0].slot_id
        session.commit()
    _enable_synthetic_sms_t2(
        monkeypatch,
        factory,
        f"slot-eval-{abs(hash(selection_text))}",
    )

    monkeypatch.setattr(inbound_messages, "interpret_inbound_text", _fake_text_intent_for_eval)
    monkeypatch.setattr(sms, "get_sessionmaker", lambda: factory)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_ALLOW_UNSIGNED_WEBHOOKS", "true")
    client = TestClient(_sms_test_app())

    offer = _post_twilio_sms(
        client,
        message_sid=f"SM-eval-offer-{selection_text}",
        from_address="+447700900001",
        body="need an appointment asap",
    )
    booking = _post_twilio_sms(
        client,
        message_sid=f"SM-eval-select-{selection_text}",
        from_address="+447700900001",
        body=selection_text,
    )

    assert "These appointment times are available" in offer.text
    assert "not yet confirmed" in booking.text
    assert "You're booked" not in booking.text
    with factory() as session:
        action = session.execute(select(BookingAction)).scalar_one()
        assert action.status == BookingActionStatus.PENDING
        assert action.availability_slot_id == slot_id