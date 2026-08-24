"""Deterministic inbound reply parsing and routing."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from ..db import clinic_scope, tenant_select
from ..enums import (
    AuditAction,
    CampaignType,
    Channel,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    InteractionDirection,
    InteractionIntent,
    InteractionOutcome,
    OutreachState,
)
from ..handoffs import ensure_handoff_receipt
from ..models import Campaign, Escalation, Interaction, OutreachJob, Patient
from ..rights import assert_patient_writable
from .audit import audit_action
from .opt_out import record_opt_out

URGENT_TERMS = {
    "urgent",
    "emergency",
    "chest pain",
    "can't breathe",
    "cannot breathe",
    "suicidal",
    "suicide",
    "bleeding",
    "fainted",
}
CLINICAL_TERMS = {
    "cough",
    "coughing",
    "cugh",
    "high blood pressure",
    "headache",
    "headaches",
    "pain",
    "rash",
    "rashes",
    "hurt",
    "hurts",
    "dizzy",
    "symptom",
    "symptoms",
    "medicine",
    "medication",
    "infection",
    "swollen",
    "sick",
    "unwell",
    "nausea",
    "vomiting",
    "bleed",
    "fever",
}
OPT_OUT_TERMS = {"stop", "unsubscribe", "remove me", "opt out", "cancel messages"}
REBOOK_TERMS = {"yes", "rebook", "book", "reschedule", "slot"}
APPOINTMENT_REQUEST_TERMS = {
    "appointment",
    "appointments",
    "availability",
    "available",
    "consultation",
    "consultations",
    "interview",
    "interviews",
    "visit",
    "visits",
}
APPOINTMENT_REQUEST_CUES = {
    "arrange",
    "asap",
    "book",
    "earliest",
    "get",
    "make",
    "need",
    "next available",
    "schedule",
    "soon",
    "want",
}
DECLINE_TERMS = {"no", "decline", "not interested", "don't want", "do not want"}
QUESTION_TERMS = {"?", "what", "when", "where", "how", "which", "who"}
COMPLAINT_TERMS = {"complain", "complaint", "awful", "terrible", "poor", "bad", "unhappy", "rude"}
NEGATIVE_TERMS = {"not happy", "worse", "disappointed", "upset", "angry", "unacceptable"}
ACKNOWLEDGEMENT_TERMS = {
    "good afternoon",
    "good evening",
    "good morning",
    "heya",
    "hey",
    "hello",
    "hello there",
    "hi",
    "hii",
    "hiii",
    "hiya",
    "yes",
    "yep",
    "yeah",
    "yo",
    "okay",
    "ok",
    "sure",
    "that works",
    "yes we can",
    "go ahead",
    "are you there",
    "are you still there",
    "can you hear me",
    "hello are you there",
    "you there",
}


@dataclass(frozen=True)
class InboundResult:
    """Outcome of routing one inbound patient reply."""

    intent: InteractionIntent
    outcome: InteractionOutcome
    escalated: bool = False
    outreach_job_id: str | None = None


@dataclass(frozen=True)
class FeedbackClassification:
    """Deterministic classification for one feedback reply."""

    intent: InteractionIntent
    outcome: InteractionOutcome
    escalated_reason: EscalationReason | None = None
    rating: int | None = None


def classify_intent(body: str) -> InteractionIntent:
    """Classify an inbound reply using deterministic safety-first rules."""
    text = _normalise(body)
    if _contains_any(text, URGENT_TERMS):
        return InteractionIntent.URGENT
    if _contains_any(text, CLINICAL_TERMS):
        return InteractionIntent.CLINICAL
    if _contains_any(text, OPT_OUT_TERMS):
        return InteractionIntent.OPT_OUT
    if _contains_any(text, REBOOK_TERMS):
        return InteractionIntent.REBOOK
    if _is_appointment_request(text):
        return InteractionIntent.REBOOK
    if _contains_any(text, DECLINE_TERMS):
        return InteractionIntent.DECLINE
    if _contains_any(text, QUESTION_TERMS):
        return InteractionIntent.QUESTION
    return InteractionIntent.UNCLEAR


def _is_appointment_request(text: str) -> bool:
    if _contains_any(text, QUESTION_TERMS):
        return False
    return _contains_any(text, APPOINTMENT_REQUEST_TERMS) and _contains_any(text, APPOINTMENT_REQUEST_CUES)


def is_conversational_acknowledgement(body: str) -> bool:
    """Return true for benign live-call greetings and acknowledgements."""
    text = _normalise(body).strip(" .!?,")
    if not text:
        return False
    if _contains_any(text, URGENT_TERMS | CLINICAL_TERMS | OPT_OUT_TERMS):
        return False
    return text in ACKNOWLEDGEMENT_TERMS


def classify_feedback(body: str) -> FeedbackClassification:
    """Classify post-visit feedback with safety-first deterministic rules."""
    intent = classify_intent(body)
    if intent == InteractionIntent.URGENT:
        return FeedbackClassification(
            intent=InteractionIntent.URGENT,
            outcome=InteractionOutcome.ROUTED_TO_STAFF,
            escalated_reason=EscalationReason.URGENT,
            rating=_rating(body),
        )
    if intent == InteractionIntent.CLINICAL:
        return FeedbackClassification(
            intent=InteractionIntent.CLINICAL,
            outcome=InteractionOutcome.ROUTED_TO_STAFF,
            escalated_reason=EscalationReason.CLINICAL,
            rating=_rating(body),
        )
    if intent == InteractionIntent.OPT_OUT:
        return FeedbackClassification(
            intent=InteractionIntent.OPT_OUT,
            outcome=InteractionOutcome.AUTO_HANDLED,
            rating=_rating(body),
        )

    text = _normalise(body)
    rating = _rating(body)
    if rating is not None and rating <= 2:
        return FeedbackClassification(
            intent=InteractionIntent.FEEDBACK,
            outcome=InteractionOutcome.ROUTED_TO_STAFF,
            escalated_reason=EscalationReason.COMPLAINT,
            rating=rating,
        )
    if _contains_any(text, COMPLAINT_TERMS | NEGATIVE_TERMS):
        return FeedbackClassification(
            intent=InteractionIntent.FEEDBACK,
            outcome=InteractionOutcome.ROUTED_TO_STAFF,
            escalated_reason=EscalationReason.COMPLAINT,
            rating=rating,
        )
    if intent in {InteractionIntent.QUESTION, InteractionIntent.UNCLEAR} and rating is None:
        return FeedbackClassification(
            intent=InteractionIntent.UNCLEAR,
            outcome=InteractionOutcome.ROUTED_TO_STAFF,
            escalated_reason=EscalationReason.AMBIGUOUS,
        )
    return FeedbackClassification(
        intent=InteractionIntent.FEEDBACK,
        outcome=InteractionOutcome.AUTO_HANDLED,
        rating=rating,
    )


def handle_inbound_reply(
    session: Session,
    *,
    clinic_id: str,
    from_address: str,
    channel: Channel,
    body: str,
    now: datetime,
) -> InboundResult:
    """Persist and route one inbound SMS/email reply for a scoped clinic."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (got naive datetime)")

    intent = classify_intent(body)
    with clinic_scope(session, clinic_id):
        patient = _find_patient(session, channel, from_address)
        if patient is None:
            raise LookupError("inbound reply did not match a patient in this clinic")
        assert_patient_writable(session, clinic_id, patient.id)
        job = _find_active_job(session, patient.id, channel)
        if job is None:
            raise LookupError("inbound reply did not match an active outreach job")

        campaign = session.get(Campaign, job.campaign_id)
        if campaign is not None and campaign.type == CampaignType.FEEDBACK:
            return _handle_feedback_reply(session, clinic_id, patient, job, channel, body, now)

        outcome = _outcome_for_intent(intent)
        interaction = Interaction(
            id=f"interaction-{uuid.uuid4().hex}",
            clinic_id=clinic_id,
            outreach_job_id=job.id,
            channel=channel,
            direction=InteractionDirection.INBOUND,
            content=body,
            intent=intent,
            outcome=outcome,
            occurred_at=now,
        )
        session.add(interaction)

        escalated = False
        if intent == InteractionIntent.OPT_OUT:
            record_opt_out(session, clinic_id, patient, channel, now)
            job.state = OutreachState.COMPLETED
        elif intent in {InteractionIntent.CLINICAL, InteractionIntent.URGENT}:
            _escalate(
                session,
                clinic_id,
                patient.id,
                job,
                interaction.id,
                intent,
                now,
            )
            job.state = OutreachState.ESCALATED
            escalated = True
        elif intent in {InteractionIntent.UNCLEAR, InteractionIntent.QUESTION}:
            _escalate(
                session,
                clinic_id,
                patient.id,
                job,
                interaction.id,
                InteractionIntent.UNCLEAR,
                now,
            )
            job.state = OutreachState.ESCALATED
            escalated = True
        elif intent == InteractionIntent.REBOOK:
            job.state = OutreachState.REPLIED
        elif intent == InteractionIntent.DECLINE:
            job.state = OutreachState.COMPLETED

        session.flush()
        return InboundResult(
            intent=intent,
            outcome=outcome,
            escalated=escalated,
            outreach_job_id=job.id,
        )


def _normalise(body: str) -> str:
    return re.sub(r"\s+", " ", body.strip().lower())


def _contains_any(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def _rating(body: str) -> int | None:
    match = re.search(r"(?<!\d)([1-5])(?!\d)", body)
    return int(match.group(1)) if match else None


def _find_patient(session: Session, channel: Channel, address: str) -> Patient | None:
    column = Patient.phone if channel == Channel.SMS else Patient.email
    return session.execute(tenant_select(Patient).where(column == address)).scalar_one_or_none()


def _find_active_job(session: Session, patient_id: str, channel: Channel) -> OutreachJob | None:
    return session.execute(
        tenant_select(OutreachJob)
        .where(
            OutreachJob.patient_id == patient_id,
            OutreachJob.channel == channel,
            OutreachJob.state.in_(
                [OutreachState.SENT, OutreachState.DELIVERED, OutreachState.QUEUED]
            ),
        )
        .order_by(OutreachJob.created_at.desc(), OutreachJob.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _outcome_for_intent(intent: InteractionIntent) -> InteractionOutcome:
    if intent in {InteractionIntent.CLINICAL, InteractionIntent.URGENT, InteractionIntent.UNCLEAR, InteractionIntent.QUESTION}:
        return InteractionOutcome.ROUTED_TO_STAFF
    return InteractionOutcome.AUTO_HANDLED


def _handle_feedback_reply(
    session: Session,
    clinic_id: str,
    patient: Patient,
    job: OutreachJob,
    channel: Channel,
    body: str,
    now: datetime,
) -> InboundResult:
    classification = classify_feedback(body)
    interaction = Interaction(
        id=f"interaction-{uuid.uuid4().hex}",
        clinic_id=clinic_id,
        outreach_job_id=job.id,
        channel=channel,
        direction=InteractionDirection.INBOUND,
        content=body,
        intent=classification.intent,
        outcome=classification.outcome,
        occurred_at=now,
    )
    session.add(interaction)
    audit_action(
        session,
        clinic_id,
        AuditAction.RECORD_FEEDBACK,
        interaction.id,
        {
            "outreach_job_id": job.id,
            "rating": classification.rating,
            "intent": classification.intent.value,
            "outcome": classification.outcome.value,
            "escalated_reason": classification.escalated_reason.value
            if classification.escalated_reason
            else None,
        },
    )

    escalated = False
    if classification.intent == InteractionIntent.OPT_OUT:
        record_opt_out(session, clinic_id, patient, channel, now)
        job.state = OutreachState.COMPLETED
    elif classification.escalated_reason is not None:
        _escalate_for_reason(
            session,
            clinic_id,
            patient.id,
            job,
            interaction.id,
            classification.intent,
            classification.escalated_reason,
            now,
        )
        job.state = OutreachState.ESCALATED
        escalated = True
    else:
        job.state = OutreachState.COMPLETED

    session.flush()
    return InboundResult(
        intent=classification.intent,
        outcome=classification.outcome,
        escalated=escalated,
        outreach_job_id=job.id,
    )


def _escalate(
    session: Session,
    clinic_id: str,
    patient_id: str,
    job: OutreachJob,
    interaction_id: str,
    intent: InteractionIntent,
    now: datetime,
) -> None:
    reason, priority = _escalation_mapping(intent)
    escalation = Escalation(
        id=f"escalation-{uuid.uuid4().hex}",
        clinic_id=clinic_id,
        patient_id=patient_id,
        reason=reason,
        priority=priority,
        context_ref=interaction_id,
        status=EscalationStatus.OPEN,
    )
    session.add(escalation)
    session.flush()
    ensure_handoff_receipt(session, clinic_id, escalation, now=now)
    audit_action(
        session,
        clinic_id,
        AuditAction.ESCALATE,
        escalation.id,
        {
            "outreach_job_id": job.id,
            "interaction_id": interaction_id,
            "intent": intent.value,
            "reason": reason.value,
            "priority": priority.value,
        },
    )


def _escalate_for_reason(
    session: Session,
    clinic_id: str,
    patient_id: str,
    job: OutreachJob,
    interaction_id: str,
    intent: InteractionIntent,
    reason: EscalationReason,
    now: datetime,
) -> None:
    priority = _priority_for_reason(reason)
    escalation = Escalation(
        id=f"escalation-{uuid.uuid4().hex}",
        clinic_id=clinic_id,
        patient_id=patient_id,
        reason=reason,
        priority=priority,
        context_ref=interaction_id,
        status=EscalationStatus.OPEN,
    )
    session.add(escalation)
    session.flush()
    ensure_handoff_receipt(session, clinic_id, escalation, now=now)
    audit_action(
        session,
        clinic_id,
        AuditAction.ESCALATE,
        escalation.id,
        {
            "outreach_job_id": job.id,
            "interaction_id": interaction_id,
            "intent": intent.value,
            "reason": reason.value,
            "priority": priority.value,
        },
    )


def _escalation_mapping(
    intent: InteractionIntent,
) -> tuple[EscalationReason, EscalationPriority]:
    if intent == InteractionIntent.URGENT:
        return EscalationReason.URGENT, EscalationPriority.HIGH
    if intent == InteractionIntent.CLINICAL:
        return EscalationReason.CLINICAL, EscalationPriority.HIGH
    return EscalationReason.AMBIGUOUS, EscalationPriority.NORMAL


def _priority_for_reason(reason: EscalationReason) -> EscalationPriority:
    if reason in {EscalationReason.URGENT, EscalationReason.CLINICAL, EscalationReason.COMPLAINT}:
        return EscalationPriority.HIGH
    return EscalationPriority.NORMAL