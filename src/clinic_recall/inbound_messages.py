"""Minimized cold inbound SMS persistence and deterministic routing."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.orm import Session

from .availability import AvailabilitySlotSummary, get_availability
from .booking import book_inbound_slot
from .db import clinic_scope, tenant_select
from .enums import (
    AppointmentStatus,
    AuditAction,
    Channel,
    IdentityTier,
    InboundMessageStatus,
    InboundStaffTaskKind,
    InteractionDirection,
    InteractionIntent,
)
from .identity_evidence import (
    IdentityAction,
    IdentityAuthorizationContext,
    IdentityEvidenceService,
)
from .inbound_identity import resolve_single_inbound_patient_id
from .inbound_staff_tasks import create_inbound_staff_task
from .inbound_text_agent import InboundTextIntent, interpret_inbound_text
from .inbound_transport import hash_phone_number_for_clinic
from .messaging.audit import audit_action
from .messaging.inbound import classify_intent, is_conversational_acknowledgement
from .messaging.opt_out import record_opt_out
from .messaging.resolve import InboundSmsRoute
from .models import Appointment, InboundMessage, Patient
from .rights import SubjectFrozenError, assert_patient_writable

CALLBACK_TERMS = {
    "call me",
    "call back",
    "callback",
    "phone me",
    "ring me",
    "reception call",
    "can reception call",
}
BOOKING_TERMS = {
    "book",
    "booking",
    "appointment",
    "consultation",
    "interview",
    "rebook",
    "reschedule",
    "slot",
    "available",
    "visit",
}
COMPLAINT_TERMS = {"complaint", "complain", "rude", "unhappy", "angry", "upset", "unacceptable"}
SAFEGUARDING_TERMS = {"abuse", "abused", "unsafe", "harm", "neglect", "domestic violence", "at risk"}
DISTRESS_TERMS = {"distress", "panic", "scared", "afraid", "desperate", "crisis", "can't cope", "cannot cope"}
THANKS_TERMS = {"thanks", "thank you", "thanks very much", "thank you very much", "cheers"}
WELLBEING_QUESTIONS = {"how are you", "how are you doing", "are you ok", "are you okay", "you ok"}
ASSISTANT_IDENTITY_QUESTIONS = {
    "who are you",
    "what are you",
    "are you a bot",
    "is this a bot",
    "are you ai",
    "is this ai",
    "are you human",
}
ASSISTANT_CAPABILITY_QUESTIONS = {
    "what can you do",
    "what do you do",
    "how can you help",
    "what can you help with",
}
FOLLOW_ON_CHITCHAT_TERMS = {"cool", "ok thanks", "okay thanks", "nice", "sounds good"}
BOOKING_INTAKE_INTENT = "booking_intake"
BOOKING_INTAKE_AWAITING_KIND = "awaiting_booking_kind"
BOOKING_INTAKE_AWAITING_PREFERENCE = "awaiting_preference"
BOOKING_INTAKE_AWAITING_SLOT = "awaiting_slot_selection"
BOOKING_INTAKE_SLOT_SELECTED = "slot_selected"
BOOKING_INTAKE_CONFIRMED = "booking_confirmed"
BOOKING_INTAKE_PENDING = "booking_pending"
BOOKING_INTAKE_READY_FOR_STAFF = "ready_for_staff_request"
NEW_BOOKING_TERMS = {"1", "new", "new appointment", "book", "booking", "appointment"}
CHANGE_BOOKING_TERMS = {
    "2",
    "change",
    "change appointment",
    "change existing",
    "existing",
    "existing appointment",
    "move",
    "reschedule",
    "rebook",
}
CALLBACK_CHOICE_TERMS = {"3", "callback", "call me", "call back", "reception call", "phone me", "ring me"}
BOOKING_PREFERENCE_BUCKETS = {
    "next_available": {"1", "as soon as possible", "asap", "earliest", "first available", "next available", "soonest"},
    "morning": {"2", "am", "morning"},
    "afternoon": {"3", "afternoon", "pm"},
    "evening": {"4", "evening"},
    "today": {"today"},
    "tomorrow": {"tomorrow"},
    "this_week": {"this week"},
    "next_week": {"next week"},
}
SLOT_ORDINAL_REFS = {
    "first": "1",
    "1st": "1",
    "second": "2",
    "2nd": "2",
    "third": "3",
    "3rd": "3",
    "fourth": "4",
    "4th": "4",
    "fifth": "5",
    "5th": "5",
}
SINGLE_SLOT_CONFIRMATION_TERMS = {
    "book",
    "book it",
    "book me",
    "book that",
    "book this",
    "confirm",
    "fine",
    "go ahead",
    "go for it",
    "looks good",
    "ok",
    "okay",
    "sounds good",
    "that one",
    "that time",
    "that works",
    "this works",
    "works",
    "yes",
    "yes please",
    "yep",
}
_IDENTITY_FACTOR_PATTERN = re.compile(
    r"\b(?:my name is|my name's|date of birth|dob|i was born|born on)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ColdInboundSmsClassification:
    """Deterministic staff-routing classification for one cold inbound SMS."""

    intent: str
    kind: InboundStaffTaskKind | None
    priority: str
    reason: str
    summary: str
    also_create_booking_request: bool = False
    reply_message: str | None = None
    conversation_payload: dict[str, object] | None = None
    task_payload: dict[str, object] | None = None


@dataclass(frozen=True)
class ColdInboundSmsResult:
    """Outcome of recording and routing one cold inbound SMS."""

    message_id: str
    intent: str
    task_id: str | None = None
    kind: InboundStaffTaskKind | None = None
    patient_id: str | None = None
    reply_message: str | None = None
    booking_stage: str | None = None
    booked: bool = False


@dataclass(frozen=True)
class SmsConversationContext:
    """Minimized previous SMS context for deterministic state transitions."""

    previous_chitchat_turns: int = 0
    booking_stage: str | None = None
    booking_kind: str | None = None
    preference_bucket: str | None = None
    patient_id: str | None = None
    identity_status: str | None = None
    identity_tier: IdentityTier = IdentityTier.T0
    identity_policy_available: bool = False
    identity_evidence_id: str | None = None
    sms_consent: bool | None = None
    sms_opted_out: bool | None = None
    appointment_id: str | None = None
    appointment_count: int = 0
    slot_offer: tuple[dict[str, str], ...] = ()
    attempt_count: int = 0
    prior_message_ids: tuple[str, ...] = ()


def handle_cold_inbound_sms(
    session: Session,
    *,
    route: InboundSmsRoute,
    provider_message_id: str | None,
    from_address: str,
    body: str,
    now: datetime,
    identity_service: IdentityEvidenceService | None = None,
    identity_context: IdentityAuthorizationContext | None = None,
) -> ColdInboundSmsResult:
    """Persist and route an inbound SMS without trusted outreach-job context."""
    _require_aware("now", now)
    message_id = _provider_message_id(route, provider_message_id, from_address, body, now)
    with clinic_scope(session, route.clinic_id):
        message = record_inbound_message(
            session,
            route=route,
            provider_message_id=message_id,
            from_address=from_address,
            body=body,
        )
        conversation_context = _sms_conversation_context(
            session,
            route=route,
            from_address=from_address,
            current_message_id=message.id,
            identity_service=identity_service,
            identity_context=identity_context,
        )
        text_intent = _interpret_safe_inbound_sms_text(body, conversation_context)
        classification = classify_cold_inbound_sms(
            body,
            conversation_context=conversation_context,
            text_intent=text_intent,
        )
        classification, booked = _enrich_booking_classification(
            session,
            route=route,
            classification=classification,
            context=conversation_context,
            now=now,
            body=body,
            identity_service=identity_service,
            identity_context=identity_context,
        )
        message.intent = classification.intent
        message.summary = classification.summary
        message.payload = _message_payload(classification, conversation_context)
        patient = _find_sms_patient(session, from_address)

        if classification.intent == InteractionIntent.OPT_OUT.value and patient is not None:
            record_opt_out(session, route.clinic_id, patient, Channel.SMS, now)
            message.status = InboundMessageStatus.ROUTED
            audit_action(
                session,
                route.clinic_id,
                AuditAction.OPT_OUT_PATIENT,
                message.id,
                {"channel": Channel.SMS.value, "message_id": message.id, "occurred_at": now},
                actor="system:clinic-sms-assistant",
            )
            session.flush()
            return ColdInboundSmsResult(
                message_id=message.id,
                intent=classification.intent,
                patient_id=patient.id,
                reply_message=classification.reply_message,
                booking_stage=_booking_stage(classification),
                booked=booked,
            )

        if classification.intent == InteractionIntent.OPT_OUT.value:
            classification = ColdInboundSmsClassification(
                intent=InteractionIntent.OPT_OUT.value,
                kind=InboundStaffTaskKind.IDENTITY_UNCLEAR,
                priority="normal",
                reason="opt_out_identity_unclear",
                summary="Opt-out request from unmatched inbound SMS requires staff identity review",
            )
            message.summary = classification.summary

        if classification.kind is None:
            message.status = InboundMessageStatus.ROUTED
            session.flush()
            return ColdInboundSmsResult(
                message_id=message.id,
                intent=classification.intent,
                reply_message=classification.reply_message,
                booking_stage=_booking_stage(classification),
                booked=booked,
            )

        task_payload: dict[str, object] = {
            "channel": Channel.SMS.value,
            "intent": classification.intent,
            "message_id": message.id,
        }
        if classification.task_payload:
            task_payload.update(classification.task_payload)
        task_result = create_inbound_staff_task(
            session,
            route.clinic_id,
            inbound_message_id=message.id,
            kind=classification.kind,
            now=now,
            priority=classification.priority,
            reason=classification.reason,
            summary=classification.summary,
            payload=task_payload,
        )
        if classification.also_create_booking_request:
            create_inbound_staff_task(
                session,
                route.clinic_id,
                inbound_message_id=message.id,
                kind=InboundStaffTaskKind.BOOKING_REQUEST,
                now=now,
                priority="normal",
                reason=f"booking_request_with_{classification.reason}",
                summary=f"Booking request captured alongside {classification.reason} escalation for staff review",
                payload={
                    "channel": Channel.SMS.value,
                    "intent": "booking_request",
                    "message_id": message.id,
                    "linked_escalation_task_id": task_result.task_id,
                },
            )
        message.status = InboundMessageStatus.ROUTED
        session.flush()
        return ColdInboundSmsResult(
            message_id=message.id,
            intent=classification.intent,
            task_id=task_result.task_id,
            kind=task_result.kind,
            reply_message=classification.reply_message,
            booking_stage=_booking_stage(classification),
            booked=booked,
        )


def record_inbound_message(
    session: Session,
    *,
    route: InboundSmsRoute,
    provider_message_id: str,
    from_address: str | None,
    body: str,
) -> InboundMessage:
    """Persist a minimized inbound SMS record, idempotent by provider message id."""
    existing = session.execute(
        tenant_select(InboundMessage).where(
            InboundMessage.provider == route.provider,
            InboundMessage.provider_message_id == provider_message_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    message = InboundMessage(
        id=f"inbound-msg-{uuid.uuid4().hex}",
        clinic_id=route.clinic_id,
        clinic_phone_number_id=route.clinic_phone_number_id,
        provider=route.provider,
        provider_message_id=provider_message_id,
        to_number=route.normalized_to_number,
        from_number_hash=hash_phone_number_for_clinic(from_address, route.clinic_id),
        direction=InteractionDirection.INBOUND,
        body_length=len(body or ""),
        body_sha256=_hash_body(body or "", route.clinic_id),
        status=InboundMessageStatus.RECEIVED,
        payload={},
    )
    session.add(message)
    session.flush()
    return message


def classify_cold_inbound_sms(
    body: str,
    *,
    previous_chitchat_turns: int = 0,
    conversation_context: SmsConversationContext | None = None,
    text_intent: InboundTextIntent | None = None,
) -> ColdInboundSmsClassification:
    """Classify cold SMS into deterministic staff actions."""
    context = conversation_context or SmsConversationContext(previous_chitchat_turns=previous_chitchat_turns)
    text = _normalise(body)
    intent = classify_intent(body)
    booking_requested = _has_booking_request(text, intent)
    if intent in {InteractionIntent.URGENT, InteractionIntent.CLINICAL}:
        return ColdInboundSmsClassification(
            intent=intent.value,
            kind=InboundStaffTaskKind.ESCALATION,
            priority="high",
            reason=intent.value,
            summary="Urgent or clinical inbound SMS escalated to staff",
            also_create_booking_request=False,
        )
    if _contains_any(text, COMPLAINT_TERMS):
        return _escalation("complaint", "Complaint inbound SMS escalated to staff", False)
    if _contains_any(text, SAFEGUARDING_TERMS):
        return _escalation("safeguarding", "Safeguarding inbound SMS escalated to staff", False)
    if _contains_any(text, DISTRESS_TERMS):
        return _escalation("distress", "Distress inbound SMS escalated to staff", False)
    if intent == InteractionIntent.OPT_OUT:
        return ColdInboundSmsClassification(
            intent=InteractionIntent.OPT_OUT.value,
            kind=InboundStaffTaskKind.IDENTITY_UNCLEAR,
            priority="normal",
            reason="opt_out",
            summary="SMS opt-out request received",
        )
    if _contains_identity_factor_answer(body):
        return _identity_staff_handoff(
            context,
            reason="identity_factor_intercepted",
            summary="Inbound SMS identity factor requires staff verification",
        )
    if booking_requested and context.identity_tier == IdentityTier.T1:
        return _generic_t1_booking_request(context)
    if booking_requested and not context.identity_policy_available:
        return _identity_staff_handoff(
            context,
            reason="identity_policy_unavailable",
            summary="Inbound SMS appointment request requires staff identity verification",
        )
    agent_intake = _classification_from_text_intent(text, context, text_intent)
    if agent_intake is not None:
        return agent_intake
    if context.booking_stage == BOOKING_INTAKE_AWAITING_SLOT:
        slot_id = _selected_slot_id(text, context)
        if slot_id is not None:
            return _selected_booking_slot(context, slot_id)
    chitchat = _classify_chitchat(text, body, context.previous_chitchat_turns)
    if chitchat is not None:
        return chitchat
    booking_intake = _classify_booking_intake(text, context)
    if booking_intake is not None:
        return booking_intake
    if _contains_any(text, CALLBACK_TERMS):
        return ColdInboundSmsClassification(
            intent="callback",
            kind=InboundStaffTaskKind.CALLBACK,
            priority="normal",
            reason="callback_request",
            summary="Callback request from inbound SMS",
        )
    if booking_requested:
        return _start_booking_intake(context)
    return ColdInboundSmsClassification(
        intent=InteractionIntent.UNCLEAR.value,
        kind=InboundStaffTaskKind.IDENTITY_UNCLEAR,
        priority="normal",
        reason="identity_unclear",
        summary="Unclear inbound SMS requires staff identity review",
    )


def _interpret_safe_inbound_sms_text(
    body: str,
    context: SmsConversationContext,
) -> InboundTextIntent | None:
    text = _normalise(body)
    intent = classify_intent(body)
    if _contains_identity_factor_answer(body):
        return None
    if _has_booking_request(text, intent) and not context.identity_policy_available:
        return None
    if intent in {InteractionIntent.URGENT, InteractionIntent.CLINICAL, InteractionIntent.OPT_OUT}:
        return None
    if _contains_any(text, COMPLAINT_TERMS | SAFEGUARDING_TERMS | DISTRESS_TERMS):
        return None
    try:
        return interpret_inbound_text(
            body=body,
            context_summary=_text_agent_context_summary(context),
            offered_slots=context.slot_offer,
        )
    except Exception:  # noqa: BLE001 - inbound SMS must fail closed to deterministic routing
        return None


def _text_agent_context_summary(context: SmsConversationContext) -> dict[str, object]:
    summary: dict[str, object] = {
        "booking_stage": context.booking_stage,
        "booking_kind": context.booking_kind,
        "preference_bucket": context.preference_bucket,
        "offered_slot_count": len(context.slot_offer),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _classification_from_text_intent(
    text: str,
    context: SmsConversationContext,
    text_intent: InboundTextIntent | None,
) -> ColdInboundSmsClassification | None:
    if text_intent is None:
        return None
    if text_intent.callback_requested or text_intent.intent == "callback":
        if context.booking_stage:
            return _callback_from_booking_intake(context)
        return ColdInboundSmsClassification(
            intent="callback",
            kind=InboundStaffTaskKind.CALLBACK,
            priority="normal",
            reason="callback_request",
            summary="Callback request from inbound SMS",
            reply_message="Thanks. A member of the clinic team will call you back.",
        )
    if text_intent.intent != "booking":
        return None

    if text_intent.selected_slot_ref:
        slot_id = _selected_slot_id_from_ref(text_intent.selected_slot_ref, context)
        if slot_id is not None:
            return _selected_booking_slot(context, slot_id)
        return _reask_or_staff_review(
            context,
            stage=context.booking_stage or BOOKING_INTAKE_AWAITING_SLOT,
            reply_message="I couldn't match that to one of the times I sent. Which appointment time works best?",
            reason="slot_selection_unclear",
            summary="Unclear SMS slot selection requires staff review",
        )
    if context.booking_stage == BOOKING_INTAKE_AWAITING_SLOT:
        slot_id = _selected_slot_id(text, context)
        if slot_id is not None:
            return _selected_booking_slot(context, slot_id)
        return None

    booking_kind = text_intent.booking_kind or context.booking_kind or _booking_kind(text)
    preference_bucket = _booking_preference_bucket_from_text_intent(text_intent) or _booking_preference_bucket(text)
    if booking_kind and preference_bucket:
        return _request_slot_offer(context, preference_bucket, booking_kind=booking_kind)
    if booking_kind:
        return _ask_booking_preference(context, booking_kind)
    return None


def _escalation(
    reason: str,
    summary: str,
    also_create_booking_request: bool = False,
) -> ColdInboundSmsClassification:
    return ColdInboundSmsClassification(
        intent=reason,
        kind=InboundStaffTaskKind.ESCALATION,
        priority="high",
        reason=reason,
        summary=summary,
        also_create_booking_request=also_create_booking_request,
    )


def _has_booking_request(text: str, intent: InteractionIntent) -> bool:
    return (intent == InteractionIntent.REBOOK and not is_conversational_acknowledgement(text)) or _contains_any(
        text, BOOKING_TERMS
    )


def _classify_booking_intake(
    text: str,
    context: SmsConversationContext,
) -> ColdInboundSmsClassification | None:
    if context.booking_stage == BOOKING_INTAKE_AWAITING_KIND:
        if _is_callback_choice(text):
            return _callback_from_booking_intake(context)
        booking_kind = _booking_kind(text)
        if booking_kind is not None:
            return _ask_booking_preference(context, booking_kind)
        return _reask_or_staff_review(
            context,
            stage=BOOKING_INTAKE_AWAITING_KIND,
            reply_message="Is this for a new appointment, changing an existing one, or would you like a callback?",
            reason="booking_kind_unclear",
            summary="Unclear SMS booking type requires staff review",
        )

    if context.booking_stage == BOOKING_INTAKE_AWAITING_PREFERENCE:
        if _is_callback_choice(text):
            return _callback_from_booking_intake(context)
        preference_bucket = _booking_preference_bucket(text)
        if preference_bucket is not None:
            return _request_slot_offer(context, preference_bucket)
        booking_kind = _booking_kind(text)
        if booking_kind is not None:
            return _ask_booking_preference(context, booking_kind)
        return _reask_or_staff_review(
            context,
            stage=BOOKING_INTAKE_AWAITING_PREFERENCE,
            reply_message="What day or time would work best for you?",
            reason="booking_preference_unclear",
            summary="Unclear SMS booking preference requires staff review",
        )

    if context.booking_stage == BOOKING_INTAKE_AWAITING_SLOT:
        slot_id = _selected_slot_id(text, context)
        if slot_id is not None:
            return _selected_booking_slot(context, slot_id)
        return _reask_or_staff_review(
            context,
            stage=BOOKING_INTAKE_AWAITING_SLOT,
            reply_message="I couldn't match that to one of the times I sent. Which appointment time works best?",
            reason="slot_selection_unclear",
            summary="Unclear SMS slot selection requires staff review",
        )

    return None


def _start_booking_intake(context: SmsConversationContext) -> ColdInboundSmsClassification:
    return ColdInboundSmsClassification(
        intent=BOOKING_INTAKE_INTENT,
        kind=None,
        priority="none",
        reason="booking_intake_started",
        summary="SMS booking intake started without staff routing",
        reply_message=(
            "I can help with that. Is this for a new appointment, changing an existing one, "
            "or would you like a callback?"
        ),
        conversation_payload=_booking_payload(
            context,
            stage=BOOKING_INTAKE_AWAITING_KIND,
            attempt_count=0,
        ),
    )


def _ask_booking_preference(context: SmsConversationContext, booking_kind: str) -> ColdInboundSmsClassification:
    return ColdInboundSmsClassification(
        intent=BOOKING_INTAKE_INTENT,
        kind=None,
        priority="none",
        reason="booking_kind_collected",
        summary="SMS booking kind collected; awaiting preferred timing",
        reply_message="Thanks. What day or time would work best for you?",
        conversation_payload=_booking_payload(
            context,
            stage=BOOKING_INTAKE_AWAITING_PREFERENCE,
            booking_kind=booking_kind,
            attempt_count=0,
        ),
    )


def _request_slot_offer(
    context: SmsConversationContext,
    preference_bucket: str,
    *,
    booking_kind: str | None = None,
) -> ColdInboundSmsClassification:
    selected_booking_kind = booking_kind or context.booking_kind or "unspecified"
    return ColdInboundSmsClassification(
        intent=BOOKING_INTAKE_INTENT,
        kind=None,
        priority="none",
        reason="booking_preference_collected",
        summary="SMS booking preference collected; preparing deterministic slot offer",
        conversation_payload=_booking_payload(
            context,
            stage=BOOKING_INTAKE_AWAITING_SLOT,
            booking_kind=selected_booking_kind,
            preference_bucket=preference_bucket,
            attempt_count=0,
        ),
    )


def _selected_booking_slot(context: SmsConversationContext, slot_id: str) -> ColdInboundSmsClassification:
    return ColdInboundSmsClassification(
        intent=BOOKING_INTAKE_INTENT,
        kind=None,
        priority="none",
        reason="slot_selected",
        summary="SMS booking slot selected; preparing deterministic booking",
        conversation_payload=_booking_payload(
            context,
            stage=BOOKING_INTAKE_SLOT_SELECTED,
            booking_kind=context.booking_kind,
            preference_bucket=context.preference_bucket,
            selected_slot_id=slot_id,
            attempt_count=0,
        ),
    )


def _callback_from_booking_intake(context: SmsConversationContext) -> ColdInboundSmsClassification:
    return ColdInboundSmsClassification(
        intent="callback",
        kind=InboundStaffTaskKind.CALLBACK,
        priority="normal",
        reason="callback_request_from_booking_intake",
        summary="Callback request from SMS booking intake",
        reply_message="Thanks. A member of the clinic team will call you back.",
        conversation_payload=_booking_payload(context, stage="callback_selected", attempt_count=0),
        task_payload={"intake_message_ids": list(context.prior_message_ids), "intake_stage": "callback_selected"},
    )


def _enrich_booking_classification(
    session: Session,
    *,
    route: InboundSmsRoute,
    classification: ColdInboundSmsClassification,
    context: SmsConversationContext,
    now: datetime,
    body: str,
    identity_service: IdentityEvidenceService | None,
    identity_context: IdentityAuthorizationContext | None,
) -> tuple[ColdInboundSmsClassification, bool]:
    stage = _booking_stage(classification)
    if classification.intent != BOOKING_INTAKE_INTENT or stage is None:
        return classification, False

    if stage == BOOKING_INTAKE_AWAITING_SLOT:
        return _prepare_slot_offer(session, route=route, classification=classification, context=context, now=now)
    if stage == BOOKING_INTAKE_SLOT_SELECTED:
        return _book_selected_slot(
            session,
            route=route,
            classification=classification,
            context=context,
            now=now,
            identity_service=identity_service,
            identity_context=identity_context,
        )
    return classification, False


def _prepare_slot_offer(
    session: Session,
    *,
    route: InboundSmsRoute,
    classification: ColdInboundSmsClassification,
    context: SmsConversationContext,
    now: datetime,
) -> tuple[ColdInboundSmsClassification, bool]:
    booking_kind = str((classification.conversation_payload or {}).get("booking_kind") or context.booking_kind or "")
    gate = _booking_gate_failure(context, booking_kind)
    if gate is not None:
        return _staff_booking_fallback(context, reason=gate[0], summary=gate[1]), False

    preference_bucket = str(
        (classification.conversation_payload or {}).get("preference_bucket") or context.preference_bucket or "next_available"
    )
    window_start, window_end = _availability_window(preference_bucket, now)
    try:
        slots = get_availability(
            session,
            route.clinic_id,
            now=now,
            window_start=window_start,
            window_end=window_end,
            limit=5,
        )
    except Exception:
        return _staff_booking_fallback(
            context,
            reason="availability_lookup_failed",
            summary="SMS booking availability lookup failed",
        ), False
    if not slots:
        return _staff_booking_fallback(
            context,
            reason="no_availability",
            summary="SMS booking found no deterministic availability",
        ), False

    slot_offer = _slot_offer_payload(slots)
    return ColdInboundSmsClassification(
        intent=BOOKING_INTAKE_INTENT,
        kind=None,
        priority="none",
        reason="slot_offer_sent",
        summary="SMS booking slot offer sent",
        reply_message=_slot_offer_reply(slots),
        conversation_payload=_booking_payload(
            context,
            stage=BOOKING_INTAKE_AWAITING_SLOT,
            booking_kind=booking_kind,
            preference_bucket=preference_bucket,
            slot_offer=slot_offer,
            attempt_count=0,
        ),
    ), False


def _book_selected_slot(
    session: Session,
    *,
    route: InboundSmsRoute,
    classification: ColdInboundSmsClassification,
    context: SmsConversationContext,
    now: datetime,
    identity_service: IdentityEvidenceService | None,
    identity_context: IdentityAuthorizationContext | None,
) -> tuple[ColdInboundSmsClassification, bool]:
    booking_kind = str((classification.conversation_payload or {}).get("booking_kind") or context.booking_kind or "")
    gate = _booking_gate_failure(context, booking_kind)
    if gate is not None:
        return _staff_booking_fallback(context, reason=gate[0], summary=gate[1]), False

    selected_slot_id = str((classification.conversation_payload or {}).get("selected_slot_id") or "")
    if not selected_slot_id:
        return _staff_booking_fallback(
            context,
            reason="slot_selection_unclear",
            summary="SMS booking slot selection was missing",
        ), False
    booking_action_type = "book" if booking_kind == "new" else "reschedule"
    appointment_id = context.appointment_id if booking_action_type == "reschedule" else None
    result = book_inbound_slot(
        session,
        route.clinic_id,
        patient_id=context.patient_id or "",
        appointment_id=appointment_id,
        slot_id=selected_slot_id,
        now=now,
        action_type=booking_action_type,
        require_staff_approval=True,
        identity_service=identity_service,
        identity_context=identity_context,
    )
    if not result.success:
        return _staff_booking_fallback(
            context,
            reason=result.error or "booking_failed",
            summary="SMS deterministic booking failed",
        ), False

    return ColdInboundSmsClassification(
        intent="booking_pending",
        kind=InboundStaffTaskKind.BOOKING_REQUEST,
        priority="normal",
        reason="booking_not_confirmed",
        summary="SMS selected time recorded for staff-owned booking follow-up",
        reply_message=(
            f"I've recorded {_slot_time_for_reply(context, selected_slot_id)} as your "
            "selected time, but it is not yet confirmed. The clinic team will follow up."
        ),
        conversation_payload=_booking_payload(
            context,
            stage=BOOKING_INTAKE_PENDING,
            booking_kind=booking_kind,
            preference_bucket=context.preference_bucket,
            selected_slot_id=selected_slot_id,
            attempt_count=0,
        ),
        task_payload={
            "booking_action_id": result.booking_action_id,
            "selected_slot_id": selected_slot_id,
            "intake_stage": BOOKING_INTAKE_PENDING,
        },
    ), False


def _booking_gate_failure(context: SmsConversationContext, booking_kind: str) -> tuple[str, str] | None:
    if context.identity_status == "no_match":
        return "identity_no_match", "SMS booking sender did not match a patient"
    if context.identity_status == "multiple_matches":
        return "identity_multiple_match", "SMS booking sender matched multiple patients"
    if context.identity_status != "single_match" or not context.patient_id:
        return "identity_unclear", "SMS booking sender identity is unclear"
    if not context.identity_policy_available:
        return (
            "identity_policy_unavailable",
            "SMS booking requires staff review because identity policy is unavailable",
        )
    if context.identity_tier != IdentityTier.T2:
        return "identity_t2_required", "SMS booking requires current T2 evidence"
    if context.sms_opted_out:
        return "sms_opted_out", "SMS booking sender is opted out of SMS"
    if context.sms_consent is not True:
        return "sms_consent_missing", "SMS booking sender lacks SMS consent"
    if booking_kind == "change_existing" and not context.appointment_id:
        return "appointment_ambiguous", "SMS booking could not resolve exactly one appointment"
    return None


def _staff_booking_fallback(
    context: SmsConversationContext,
    *,
    reason: str,
    summary: str,
) -> ColdInboundSmsClassification:
    if reason.startswith("identity_"):
        return _identity_staff_handoff(context, reason=reason, summary=summary)
    booking_kind = context.booking_kind or "unspecified"
    task_payload = {
        "booking_kind": booking_kind,
        "preference_bucket": context.preference_bucket,
        "intake_message_ids": list(context.prior_message_ids),
        "intake_stage": BOOKING_INTAKE_READY_FOR_STAFF,
        "fallback_reason": reason,
    }
    if context.identity_tier == IdentityTier.T2 and context.patient_id:
        task_payload["patient_id"] = context.patient_id
    if context.identity_tier == IdentityTier.T2 and context.appointment_id:
        task_payload["appointment_id"] = context.appointment_id
    return ColdInboundSmsClassification(
        intent="booking_request",
        kind=InboundStaffTaskKind.BOOKING_REQUEST,
        priority="normal",
        reason=reason,
        summary=summary,
        reply_message="I couldn't safely confirm that by text, so I've sent it to the clinic team to confirm.",
        conversation_payload=_booking_payload(
            context,
            stage=BOOKING_INTAKE_READY_FOR_STAFF,
            booking_kind=booking_kind,
            preference_bucket=context.preference_bucket,
            attempt_count=0,
        ),
        task_payload=task_payload,
    )


def _identity_staff_handoff(
    context: SmsConversationContext,
    *,
    reason: str,
    summary: str,
) -> ColdInboundSmsClassification:
    return ColdInboundSmsClassification(
        intent="identity_unclear",
        kind=InboundStaffTaskKind.IDENTITY_UNCLEAR,
        priority="normal",
        reason=reason,
        summary=summary,
        reply_message=(
            "I can't verify identity by text, so I won't discuss or record appointment "
            "details. The clinic team will follow up."
        ),
        conversation_payload={
            "stage": BOOKING_INTAKE_READY_FOR_STAFF,
            "attempt_count": 0,
            "prior_message_ids": list(context.prior_message_ids),
        },
        task_payload={
            "fallback_reason": reason,
            "intake_message_ids": list(context.prior_message_ids),
        },
    )


def _generic_t1_booking_request(
    context: SmsConversationContext,
) -> ColdInboundSmsClassification:
    return ColdInboundSmsClassification(
        intent="booking_request",
        kind=InboundStaffTaskKind.BOOKING_REQUEST,
        priority="normal",
        reason="identity_t1_generic_request",
        summary="Generic inbound SMS booking request requires staff confirmation",
        reply_message="I've sent a generic appointment request to the clinic team to follow up.",
        conversation_payload={
            "stage": BOOKING_INTAKE_READY_FOR_STAFF,
            "attempt_count": 0,
            "prior_message_ids": list(context.prior_message_ids),
        },
        task_payload={
            "fallback_reason": "identity_t1_generic_request",
            "intake_message_ids": list(context.prior_message_ids),
        },
    )


def _reask_or_staff_review(
    context: SmsConversationContext,
    *,
    stage: str,
    reply_message: str,
    reason: str,
    summary: str,
) -> ColdInboundSmsClassification:
    attempt_count = context.attempt_count + 1
    if attempt_count <= 1:
        return ColdInboundSmsClassification(
            intent=BOOKING_INTAKE_INTENT,
            kind=None,
            priority="none",
            reason=reason,
            summary=summary,
            reply_message=reply_message,
            conversation_payload=_booking_payload(context, stage=stage, attempt_count=attempt_count),
        )
    return ColdInboundSmsClassification(
        intent=InteractionIntent.UNCLEAR.value,
        kind=InboundStaffTaskKind.IDENTITY_UNCLEAR,
        priority="normal",
        reason=reason,
        summary=summary,
        task_payload={"intake_message_ids": list(context.prior_message_ids), "intake_stage": stage},
    )


def _booking_kind(text: str) -> str | None:
    clean = _option_text(text)
    if _matches_option(clean, CHANGE_BOOKING_TERMS):
        return "change_existing"
    if _matches_option(clean, NEW_BOOKING_TERMS):
        return "new"
    return None


def _booking_preference_bucket(text: str) -> str | None:
    clean = _option_text(text)
    for bucket, terms in BOOKING_PREFERENCE_BUCKETS.items():
        if _matches_option(clean, terms):
            return bucket
    return None


def _booking_preference_bucket_from_text_intent(text_intent: InboundTextIntent) -> str | None:
    preference = _option_text(text_intent.time_preference or "")
    if not preference:
        return None
    direct = _booking_preference_bucket(preference)
    if direct is not None:
        return direct
    if "earliest" in preference or "soonest" in preference:
        return "next_available"
    if "morning" in preference or preference.endswith("_am") or "_morning" in preference:
        return "morning"
    if "afternoon" in preference or "_afternoon" in preference:
        return "afternoon"
    if "evening" in preference or "_evening" in preference:
        return "evening"
    if "next week" in preference or preference.startswith("next_"):
        return "next_week"
    if _contains_any(preference, {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}):
        return "this_week"
    return None


def _selected_slot_id(text: str, context: SmsConversationContext) -> str | None:
    clean = _option_text(text)
    if clean.isdigit():
        return _selected_slot_id_from_ref(clean, context)
    ordinal_ref = _slot_ref_from_ordinal(clean)
    if ordinal_ref is not None:
        return _selected_slot_id_from_ref(ordinal_ref, context)
    time_match = _selected_slot_id_from_time_text(clean, context)
    if time_match is not None:
        return time_match
    if len(context.slot_offer) == 1 and _contains_any(clean, SINGLE_SLOT_CONFIRMATION_TERMS):
        return context.slot_offer[0].get("slot_id")
    return None


def _selected_slot_id_from_ref(selected_slot_ref: str, context: SmsConversationContext) -> str | None:
    for offer in context.slot_offer:
        if offer.get("ref") == selected_slot_ref:
            return offer.get("slot_id")
    return None


def _slot_ref_from_ordinal(text: str) -> str | None:
    for term, ref in SLOT_ORDINAL_REFS.items():
        if re.search(rf"\b{re.escape(term)}\b", text):
            return ref
    return None


def _selected_slot_id_from_time_text(text: str, context: SmsConversationContext) -> str | None:
    matches: list[str] = []
    for offer in context.slot_offer:
        raw_start = offer.get("start_at")
        slot_id = offer.get("slot_id")
        if not raw_start or not slot_id:
            continue
        try:
            start_at = datetime.fromisoformat(raw_start)
        except ValueError:
            continue
        if any(candidate and candidate in text for candidate in _slot_time_text_candidates(start_at)):
            matches.append(slot_id)
    unique_matches = sorted(set(matches))
    if len(unique_matches) == 1:
        return unique_matches[0]
    return None


def _slot_time_text_candidates(value: datetime) -> set[str]:
    local = value.astimezone(UTC)
    hour_12 = local.hour % 12 or 12
    am_pm = "am" if local.hour < 12 else "pm"
    candidates = {
        _format_slot_time(local).lower(),
        local.strftime("%a %d %b").lower(),
        local.strftime("%A %d %B").lower(),
        local.strftime("%H:%M").lower(),
        f"{local.hour}:{local.minute:02d}",
        f"{hour_12}:{local.minute:02d}{am_pm}",
        f"{hour_12}:{local.minute:02d} {am_pm}",
    }
    if local.minute == 0:
        candidates.update({f"{hour_12}{am_pm}", f"{hour_12} {am_pm}"})
    return candidates


def _is_callback_choice(text: str) -> bool:
    return _matches_option(_option_text(text), CALLBACK_CHOICE_TERMS)


def _matches_option(text: str, terms: set[str]) -> bool:
    if text in terms:
        return True
    return any(term in text for term in terms if len(term) > 3)


def _option_text(text: str) -> str:
    clean = text.strip(" .!?,")
    for prefix in ("please ", "pls "):
        clean = clean.removeprefix(prefix)
    for suffix in (" please", " pls", " thanks", " thank you"):
        clean = clean.removesuffix(suffix)
    return clean.strip(" .!?,")


def _availability_window(preference_bucket: str, now: datetime) -> tuple[datetime, datetime]:
    current = now.astimezone(UTC)
    if preference_bucket == "today":
        return current, current.replace(hour=23, minute=59, second=59, microsecond=0)
    if preference_bucket == "tomorrow":
        start = current + timedelta(days=1)
        return start.replace(hour=0, minute=0, second=0, microsecond=0), start.replace(
            hour=23, minute=59, second=59, microsecond=0
        )
    if preference_bucket == "next_week":
        return current + timedelta(days=7), current + timedelta(days=14)
    return current, current + timedelta(days=7)


def _slot_offer_payload(slots: list[AvailabilitySlotSummary]) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "ref": str(index),
            "slot_id": slot.slot_id,
            "start_at": slot.start_at.isoformat(),
            "end_at": slot.end_at.isoformat(),
        }
        for index, slot in enumerate(slots, start=1)
    )


def _slot_offer_reply(slots: list[AvailabilitySlotSummary]) -> str:
    options = "; ".join(_format_slot_time(slot.start_at) for slot in slots)
    return f"These appointment times are available: {options}. Which one works best?"


def _slot_time_for_reply(context: SmsConversationContext, slot_id: str) -> str:
    for offer in context.slot_offer:
        if offer.get("slot_id") != slot_id:
            continue
        raw_start = offer.get("start_at")
        if raw_start:
            return _format_slot_time(datetime.fromisoformat(raw_start))
    return "the selected appointment time"


def _format_slot_time(value: datetime) -> str:
    local = value.astimezone(UTC)
    return local.strftime("%a %d %b, %H:%M UTC")


def _booking_payload(
    context: SmsConversationContext,
    *,
    stage: str,
    booking_kind: str | None = None,
    preference_bucket: str | None = None,
    slot_offer: tuple[dict[str, str], ...] | None = None,
    selected_slot_id: str | None = None,
    attempt_count: int,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "stage": stage,
        "attempt_count": attempt_count,
        "prior_message_ids": list(context.prior_message_ids),
    }
    selected_kind = booking_kind or context.booking_kind
    selected_preference = preference_bucket or context.preference_bucket
    selected_offer = slot_offer or context.slot_offer
    if selected_kind:
        payload["booking_kind"] = selected_kind
    if selected_preference:
        payload["preference_bucket"] = selected_preference
    if context.identity_tier == IdentityTier.T2 and context.patient_id:
        payload["patient_id"] = context.patient_id
    if context.identity_tier == IdentityTier.T2 and context.appointment_id:
        payload["appointment_id"] = context.appointment_id
    if selected_offer:
        payload["slot_offer"] = [dict(offer) for offer in selected_offer]
    if selected_slot_id:
        payload["selected_slot_id"] = selected_slot_id
    return payload


def _classify_chitchat(
    text: str,
    body: str,
    previous_chitchat_turns: int,
) -> ColdInboundSmsClassification | None:
    clean = text.strip(" .!?,")
    reason: str | None = None
    if is_conversational_acknowledgement(body):
        reason = "conversational_acknowledgement"
    elif clean in THANKS_TERMS:
        reason = "thanks"
    elif clean in WELLBEING_QUESTIONS:
        reason = "wellbeing_question"
    elif clean in ASSISTANT_IDENTITY_QUESTIONS:
        reason = "assistant_identity_question"
    elif clean in ASSISTANT_CAPABILITY_QUESTIONS:
        reason = "assistant_capability_question"
    elif previous_chitchat_turns > 0 and clean in FOLLOW_ON_CHITCHAT_TERMS:
        reason = "follow_on_chitchat"

    if reason is None:
        return None
    return ColdInboundSmsClassification(
        intent="chitchat",
        kind=None,
        priority="none",
        reason=reason,
        summary="Conversational inbound SMS acknowledged without staff routing",
        reply_message=_chitchat_reply(reason, previous_chitchat_turns),
    )


def _chitchat_reply(reason: str, previous_chitchat_turns: int) -> str:
    if reason == "thanks":
        return "You're welcome. I can help with appointments, callback requests, or clinic information."
    if reason == "wellbeing_question":
        return "I'm here and ready to help. Do you need an appointment, a callback, or clinic information?"
    if reason == "assistant_identity_question":
        return "I'm the clinic assistant for appointments and clinic information. How can I help?"
    if reason == "assistant_capability_question":
        return "I can help with appointment requests, callback requests, and clinic information. What do you need?"
    if previous_chitchat_turns > 0:
        return "I'm here. Send what you need about an appointment, callback, or clinic information."
    return "Hi, this is the clinic assistant. How can I help with appointments or clinic information?"


def _message_payload(
    classification: ColdInboundSmsClassification,
    context: SmsConversationContext,
) -> dict[str, object]:
    state = "awaiting_intent" if classification.intent == "chitchat" else "action_triggered"
    if classification.intent == BOOKING_INTAKE_INTENT:
        state = "booking_intake"
    payload: dict[str, object] = {
        "sms_conversation": {
            "state": state,
            "previous_chitchat_turns": context.previous_chitchat_turns,
        }
    }
    if classification.intent == "chitchat":
        payload["sms_conversation"]["turn"] = context.previous_chitchat_turns + 1
        payload["sms_conversation"]["reason"] = classification.reason
    if classification.conversation_payload:
        payload["sms_booking_intake"] = classification.conversation_payload
    return payload


def _booking_stage(classification: ColdInboundSmsClassification) -> str | None:
    if not classification.conversation_payload:
        return None
    stage = classification.conversation_payload.get("stage")
    return str(stage) if stage else None


def _sms_conversation_context(
    session: Session,
    *,
    route: InboundSmsRoute,
    from_address: str,
    current_message_id: str,
    identity_service: IdentityEvidenceService | None = None,
    identity_context: IdentityAuthorizationContext | None = None,
) -> SmsConversationContext:
    caller_hash = hash_phone_number_for_clinic(from_address, route.clinic_id)
    if not caller_hash:
        return SmsConversationContext()
    identity = resolve_single_inbound_patient_id(session, route.clinic_id, caller_hash)
    patient_id: str | None = None
    identity_tier = IdentityTier.T0
    identity_policy_available = False
    if identity.patient_id and identity_service is not None and identity_context is not None:
        generic_decision = identity_service.authorize(
            session,
            clinic_id=route.clinic_id,
            evidence_id=identity_context.evidence_id,
            session_id=identity_context.session_id,
            route_id=identity_context.route_id,
            channel=identity_context.channel,
            patient_id=identity.patient_id,
            action=IdentityAction.GENERIC_BOOKING_REQUEST,
        )
        identity_tier = generic_decision.tier
        identity_policy_available = generic_decision.policy_version is not None
        patient_read = identity_service.authorize(
            session,
            clinic_id=route.clinic_id,
            evidence_id=identity_context.evidence_id,
            session_id=identity_context.session_id,
            route_id=identity_context.route_id,
            channel=identity_context.channel,
            patient_id=identity.patient_id,
            action=IdentityAction.PATIENT_APPOINTMENT_READ,
        )
        if patient_read.allowed:
            patient_id = identity.patient_id
            identity_tier = IdentityTier.T2
    patient = session.get(Patient, patient_id) if patient_id else None
    eligible_appointments = _eligible_appointment_ids(session, patient_id)
    base_context = SmsConversationContext(
        patient_id=patient_id,
        identity_status=identity.status,
        identity_tier=identity_tier,
        identity_policy_available=identity_policy_available,
        identity_evidence_id=(identity_context.evidence_id if patient_id else None),
        sms_consent=patient.consent_flags.get("sms") is True if patient is not None else None,
        sms_opted_out=patient.opt_out_flags.get("sms") is True if patient is not None else None,
        appointment_id=eligible_appointments[0] if len(eligible_appointments) == 1 else None,
        appointment_count=len(eligible_appointments),
    )
    recent_messages = session.execute(
        tenant_select(InboundMessage)
        .where(
            InboundMessage.id != current_message_id,
            InboundMessage.from_number_hash == caller_hash,
            InboundMessage.to_number == route.normalized_to_number,
        )
        .order_by(InboundMessage.created_at.desc())
        .limit(10)
    ).scalars().all()
    previous_chitchat_turns = sum(1 for message in recent_messages if message.intent == "chitchat")
    for message in recent_messages:
        if message.intent == "chitchat":
            continue
        if message.intent != BOOKING_INTAKE_INTENT:
            break
        intake = (message.payload or {}).get("sms_booking_intake") if isinstance(message.payload, dict) else None
        if not isinstance(intake, dict):
            break
        stage = str(intake.get("stage") or "")
        if stage not in {BOOKING_INTAKE_AWAITING_KIND, BOOKING_INTAKE_AWAITING_PREFERENCE, BOOKING_INTAKE_AWAITING_SLOT}:
            break
        prior_message_ids = tuple(str(item) for item in intake.get("prior_message_ids") or ()) + (message.id,)
        return SmsConversationContext(
            previous_chitchat_turns=previous_chitchat_turns,
            booking_stage=stage,
            booking_kind=str(intake.get("booking_kind") or "") or None,
            preference_bucket=str(intake.get("preference_bucket") or "") or None,
            patient_id=base_context.patient_id,
            identity_status=base_context.identity_status,
            identity_tier=base_context.identity_tier,
            identity_policy_available=base_context.identity_policy_available,
            identity_evidence_id=base_context.identity_evidence_id,
            sms_consent=base_context.sms_consent,
            sms_opted_out=base_context.sms_opted_out,
            appointment_id=base_context.appointment_id,
            appointment_count=base_context.appointment_count,
            slot_offer=_coerce_slot_offer(intake.get("slot_offer")),
            attempt_count=int(intake.get("attempt_count") or 0),
            prior_message_ids=prior_message_ids,
        )
    return SmsConversationContext(
        previous_chitchat_turns=previous_chitchat_turns,
        patient_id=base_context.patient_id,
        identity_status=base_context.identity_status,
        identity_tier=base_context.identity_tier,
        identity_policy_available=base_context.identity_policy_available,
        identity_evidence_id=base_context.identity_evidence_id,
        sms_consent=base_context.sms_consent,
        sms_opted_out=base_context.sms_opted_out,
        appointment_id=base_context.appointment_id,
        appointment_count=base_context.appointment_count,
    )


def _eligible_appointment_ids(session: Session, patient_id: str | None) -> list[str]:
    if not patient_id:
        return []
    rows = session.execute(
        tenant_select(Appointment)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.status != AppointmentStatus.COMPLETED,
        )
        .order_by(Appointment.start_at.desc(), Appointment.id)
        .limit(3)
    ).scalars().all()
    return [appointment.id for appointment in rows]


def _coerce_slot_offer(raw: object) -> tuple[dict[str, str], ...]:
    if not isinstance(raw, list):
        return ()
    offers: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "")
        slot_id = str(item.get("slot_id") or "")
        start_at = str(item.get("start_at") or "")
        end_at = str(item.get("end_at") or "")
        if ref and slot_id and start_at and end_at:
            offers.append({"ref": ref, "slot_id": slot_id, "start_at": start_at, "end_at": end_at})
    return tuple(offers)


def _contains_identity_factor_answer(body: str) -> bool:
    return _IDENTITY_FACTOR_PATTERN.search(body or "") is not None


def _find_sms_patient(session: Session, from_address: str) -> Patient | None:
    try:
        patient = session.execute(
            tenant_select(Patient).where(Patient.phone == from_address)
        ).scalar_one_or_none()
    except MultipleResultsFound:
        return None
    if patient is None:
        return None
    try:
        assert_patient_writable(session, patient.clinic_id, patient.id)
    except SubjectFrozenError:
        return None
    return patient


def _provider_message_id(
    route: InboundSmsRoute,
    provider_message_id: str | None,
    from_address: str,
    body: str,
    now: datetime,
) -> str:
    if provider_message_id and provider_message_id.strip():
        return provider_message_id.strip()
    digest = hashlib.sha256(
        ":".join(
            [
                str(route.provider.value if route.provider else "unknown"),
                route.normalized_to_number,
                from_address,
                str(int(now.timestamp() // 60)),
                _hash_body(body, route.clinic_id),
            ]
        ).encode("utf-8")
    ).hexdigest()
    return f"generated:{digest}"


def _hash_body(body: str, clinic_id: str) -> str:
    salt = os.getenv("CLINIC_RECALL_MESSAGE_HASH_SALT") or os.getenv("CLINIC_RECALL_CALLER_HASH_SALT") or "dev-salt"
    digest = hashlib.sha256(f"{salt}:{clinic_id}:{body}".encode()).hexdigest()
    return f"sha256:{digest}"


def _normalise(body: str) -> str:
    return re.sub(r"\s+", " ", body.strip().lower())


def _contains_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def _require_aware(field: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


__all__ = [
    "ColdInboundSmsClassification",
    "ColdInboundSmsResult",
    "classify_cold_inbound_sms",
    "handle_cold_inbound_sms",
    "record_inbound_message",
]