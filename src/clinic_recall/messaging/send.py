"""Deterministic send tools for Clinic Recall outreach."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..booking_confirmation import is_provider_booking_verified
from ..candidate_queue import _patient_view, clinic_config_from_row
from ..db import clinic_scope, tenant_select
from ..eligibility import evaluate
from ..enums import (
    AuditAction,
    BookingActionStatus,
    CampaignType,
    Channel,
    InteractionDirection,
    InteractionOutcome,
    OutreachState,
    SkipReason,
)
from ..identity_evidence import IdentityEvidenceService
from ..models import (
    Appointment,
    AvailabilitySlot,
    BookingAction,
    Campaign,
    Clinic,
    Interaction,
    OutreachJob,
    Patient,
)
from ..pilot_controls import JobPilotGate, pilot_gate_decision
from ..rights import SubjectFrozenError, assert_patient_writable
from ..telemetry import queue_after_commit
from .audit import audit_action
from .history import contact_history_for_send
from .sender import (
    RETRYABLE_PROVIDER_FAILURES,
    MessageSender,
    ProviderFailureCode,
    SendResult,
    classify_provider_failure,
)
from .templates import (
    RenderedMessage,
    render_booking_confirmation_email,
    render_booking_confirmation_sms,
    render_feedback_email,
    render_feedback_sms,
    render_recall_email,
    render_recall_sms,
)

SMS_MAX_LENGTH = 459
TRANSIENT_SKIP_REASONS = {SkipReason.DAILY_CAP, SkipReason.OUTSIDE_CONTACT_HOURS, SkipReason.QUIET_HOURS}
PERMANENT_SKIP_REASONS = {SkipReason.OPTED_OUT, SkipReason.NO_CONSENT, SkipReason.NOT_CONTACTABLE}
TRANSIENT_RETRY_DELAY = timedelta(hours=1)


@dataclass(frozen=True)
class SendAttemptResult:
    """Outcome of a deterministic send service call."""

    sent: bool
    state: OutreachState
    idempotent: bool = False
    skip_reason: SkipReason | None = None
    provider_message_id: str | None = None
    error: str | None = None
    failure_code: ProviderFailureCode | None = None
    retry_at: datetime | None = None
    pilot_reason: str | None = None


def send_sms(
    session: Session,
    clinic_id: str,
    outreach_job_id: str,
    now: datetime,
    sender: MessageSender,
    *,
    pilot_gate: JobPilotGate,
    status_callback_url: str | None = None,
) -> SendAttemptResult:
    """Send one queued SMS job after re-checking all eligibility gates."""
    return _send(
        session,
        clinic_id,
        outreach_job_id,
        now,
        sender,
        Channel.SMS,
        pilot_gate=pilot_gate,
        status_callback_url=status_callback_url,
    )


def send_email(
    session: Session,
    clinic_id: str,
    outreach_job_id: str,
    now: datetime,
    sender: MessageSender,
    *,
    pilot_gate: JobPilotGate,
) -> SendAttemptResult:
    """Send one queued email job after re-checking all eligibility gates."""
    return _send(
        session,
        clinic_id,
        outreach_job_id,
        now,
        sender,
        Channel.EMAIL,
        pilot_gate=pilot_gate,
    )


def send_sms_confirmation(
    session: Session,
    clinic_id: str,
    outreach_job_id: str,
    now: datetime,
    sender: MessageSender,
    *,
    pilot_gate: JobPilotGate,
    booking_action_id: str | None = None,
    status_callback_url: str | None = None,
    identity_service: IdentityEvidenceService | None = None,
) -> SendAttemptResult:
    """Send an idempotent SMS confirmation for a completed voice booking."""
    return _send_booking_confirmation(
        session,
        clinic_id,
        outreach_job_id,
        now,
        sender,
        Channel.SMS,
        pilot_gate=pilot_gate,
        booking_action_id=booking_action_id,
        status_callback_url=status_callback_url,
        identity_service=identity_service,
    )


def send_email_confirmation(
    session: Session,
    clinic_id: str,
    outreach_job_id: str,
    now: datetime,
    sender: MessageSender,
    *,
    pilot_gate: JobPilotGate,
    booking_action_id: str | None = None,
    identity_service: IdentityEvidenceService | None = None,
) -> SendAttemptResult:
    """Send an idempotent email confirmation for a completed voice booking."""
    return _send_booking_confirmation(
        session,
        clinic_id,
        outreach_job_id,
        now,
        sender,
        Channel.EMAIL,
        pilot_gate=pilot_gate,
        booking_action_id=booking_action_id,
        identity_service=identity_service,
    )


def _send(
    session: Session,
    clinic_id: str,
    outreach_job_id: str,
    now: datetime,
    sender: MessageSender,
    channel: Channel,
    *,
    pilot_gate: JobPilotGate,
    status_callback_url: str | None = None,
) -> SendAttemptResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (got naive datetime)")

    with clinic_scope(session, clinic_id):
        clinic = session.get(Clinic, clinic_id)
        if clinic is None:
            raise LookupError(f"clinic {clinic_id!r} not found")
        job = _load_job(session, outreach_job_id, channel, for_update=True)
        if job is None:
            raise LookupError(f"outreach job {outreach_job_id!r} not found for clinic")

        if job.state != OutreachState.QUEUED:
            return SendAttemptResult(sent=False, state=job.state, idempotent=True)
        if _has_outbound_interaction(session, job.id, channel):
            return SendAttemptResult(sent=False, state=job.state, idempotent=True)
        pilot_decision = pilot_gate_decision(
            pilot_gate(session, clinic_id, job, now)
        )
        if not pilot_decision.allowed:
            audit_action(
                session,
                clinic_id,
                AuditAction.SKIP_CANDIDATE,
                job.id,
                {
                    "channel": channel.value,
                    "skip_reason": pilot_decision.reason,
                },
            )
            session.flush()
            return SendAttemptResult(
                sent=False,
                state=job.state,
                pilot_reason=pilot_decision.reason,
            )

        patient = session.get(Patient, job.patient_id)
        if patient is None:
            raise LookupError(f"patient {job.patient_id!r} not found for job")
        try:
            assert_patient_writable(session, clinic_id, patient.id)
        except SubjectFrozenError:
            audit_action(
                session,
                clinic_id,
                AuditAction.SKIP_CANDIDATE,
                job.id,
                {"channel": channel.value, "skip_reason": "subject_frozen"},
            )
            session.flush()
            return SendAttemptResult(
                sent=False,
                state=job.state,
                error="subject_frozen",
            )
        appointment = session.get(Appointment, job.appointment_id) if job.appointment_id else None
        config = clinic_config_from_row(clinic)
        history = contact_history_for_send(session, clinic_id, patient.id, now, config)
        decision = evaluate(_patient_view(patient), config, history, now, channel)
        if not decision.eligible:
            assert decision.skip_reason is not None  # nosec B101
            return _record_skip(session, clinic_id, job, now, decision.skip_reason)

        campaign = session.get(Campaign, job.campaign_id)
        campaign_type = campaign.type if campaign is not None else CampaignType.RECOVERY
        rendered = _render(channel, clinic, patient, appointment, campaign_type)
        if channel == Channel.SMS and len(rendered.body) > SMS_MAX_LENGTH:
            return _record_size_failure(session, clinic_id, job, rendered)

        recipient = _recipient(patient, channel)
        provider_result = _send_rendered(
            sender,
            channel,
            recipient,
            rendered,
            job.id,
            status_callback_url=status_callback_url,
        )
        failure_code = classify_provider_failure(provider_result)
        retryable_failure = failure_code in RETRYABLE_PROVIDER_FAILURES
        job.attempts = int(job.attempts or 0) + 1
        if provider_result.successful:
            job.state = OutreachState.SENT
        elif not retryable_failure:
            job.state = OutreachState.FAILED
        job.next_action_at = None
        if not retryable_failure:
            session.add(
                Interaction(
                    id=f"interaction-{uuid.uuid4().hex}",
                    clinic_id=clinic_id,
                    outreach_job_id=job.id,
                    channel=channel,
                    direction=InteractionDirection.OUTBOUND,
                    content=rendered.body,
                    outcome=(
                        InteractionOutcome.AUTO_HANDLED
                        if provider_result.successful
                        else InteractionOutcome.IGNORED
                    ),
                    occurred_at=now,
                )
            )
        _audit_send(session, clinic_id, job, rendered, provider_result)
        session.flush()
        queue_after_commit(
            session,
            "outreach.message.sent",
            {
                "provider": provider_result.provider,
                "channel": channel.value,
                "status": "accepted" if provider_result.successful else "failed",
                "successful": provider_result.successful,
                "message_kind": "outreach",
            },
        )
        return SendAttemptResult(
            sent=provider_result.successful,
            state=job.state,
            provider_message_id=provider_result.provider_message_id,
            error=provider_result.error,
            failure_code=failure_code,
        )


def _load_job(
    session: Session,
    outreach_job_id: str,
    channel: Channel,
    *,
    for_update: bool = False,
) -> OutreachJob | None:
    statement = tenant_select(OutreachJob).where(
        OutreachJob.id == outreach_job_id,
        OutreachJob.channel == channel,
    )
    if (
        for_update
        and session.bind is not None
        and session.bind.dialect.name == "postgresql"
    ):
        statement = statement.with_for_update()
    return session.execute(statement).scalar_one_or_none()


def _load_job_any_channel(session: Session, outreach_job_id: str) -> OutreachJob | None:
    return session.execute(
        tenant_select(OutreachJob).where(OutreachJob.id == outreach_job_id)
    ).scalar_one_or_none()


def _has_outbound_interaction(session: Session, outreach_job_id: str, channel: Channel) -> bool:
    return (
        session.execute(
            select(Interaction.id).where(
                Interaction.outreach_job_id == outreach_job_id,
                Interaction.channel == channel,
                Interaction.direction == InteractionDirection.OUTBOUND,
            )
        ).first()
        is not None
    )


def _render(
    channel: Channel,
    clinic: Clinic,
    patient: Patient,
    appointment: Appointment | None,
    campaign_type: CampaignType = CampaignType.RECOVERY,
) -> RenderedMessage:
    if campaign_type == CampaignType.FEEDBACK:
        if channel == Channel.SMS:
            return render_feedback_sms(clinic, patient, appointment)
        if channel == Channel.EMAIL:
            return render_feedback_email(clinic, patient, appointment)
    if channel == Channel.SMS:
        return render_recall_sms(clinic, patient, appointment)
    if channel == Channel.EMAIL:
        return render_recall_email(clinic, patient, appointment)
    raise ValueError(f"unsupported send channel: {channel.value}")


def _send_booking_confirmation(
    session: Session,
    clinic_id: str,
    outreach_job_id: str,
    now: datetime,
    sender: MessageSender,
    channel: Channel,
    *,
    pilot_gate: JobPilotGate,
    booking_action_id: str | None = None,
    status_callback_url: str | None = None,
    identity_service: IdentityEvidenceService | None = None,
) -> SendAttemptResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (got naive datetime)")

    with clinic_scope(session, clinic_id):
        clinic = session.get(Clinic, clinic_id)
        if clinic is None:
            raise LookupError(f"clinic {clinic_id!r} not found")
        job = _load_job_any_channel(session, outreach_job_id)
        if job is None:
            raise LookupError(f"outreach job {outreach_job_id!r} not found for clinic")
        pilot_decision = pilot_gate_decision(
            pilot_gate(session, clinic_id, job, now)
        )
        if not pilot_decision.allowed:
            return SendAttemptResult(
                sent=False,
                state=job.state,
                pilot_reason=pilot_decision.reason,
            )
        patient = session.get(Patient, job.patient_id)
        if patient is None:
            raise LookupError(f"patient {job.patient_id!r} not found for job")
        try:
            assert_patient_writable(session, clinic_id, patient.id)
        except SubjectFrozenError:
            return SendAttemptResult(
                sent=False,
                state=job.state,
                error="subject_frozen",
            )
        booking = _verified_booking_action(
            session,
            job.id,
            booking_action_id=booking_action_id,
        )
        if booking is None:
            return SendAttemptResult(
                sent=False,
                state=job.state,
                error="provider_booking_not_verified",
            )
        if not is_provider_booking_verified(
            session,
            clinic_id=clinic_id,
            action=booking,
            identity_service=identity_service,
        ):
            return SendAttemptResult(
                sent=False,
                state=job.state,
                error="provider_booking_not_verified",
            )
        slot = session.get(AvailabilitySlot, booking.availability_slot_id) if booking.availability_slot_id else None
        config = clinic_config_from_row(clinic)
        history = contact_history_for_send(session, clinic_id, patient.id, now, config)
        decision = evaluate(_patient_view(patient), config, history, now, channel)
        if not decision.eligible:
            assert decision.skip_reason is not None  # nosec B101
            return SendAttemptResult(sent=False, state=job.state, skip_reason=decision.skip_reason)
        rendered = (
            render_booking_confirmation_sms(clinic, patient, slot.start_at if slot else None)
            if channel == Channel.SMS
            else render_booking_confirmation_email(clinic, patient, slot.start_at if slot else None)
        )
        if channel == Channel.SMS and len(rendered.body) > SMS_MAX_LENGTH:
            return SendAttemptResult(sent=False, state=job.state, error="sms_size_limit")
        if _has_matching_outbound_interaction(session, job.id, channel, rendered.body):
            return SendAttemptResult(sent=False, state=job.state, idempotent=True)
        recipient = _recipient(patient, channel)
        provider_result = _send_rendered(
            sender,
            channel,
            recipient,
            rendered,
            job.id,
            status_callback_url=status_callback_url,
        )
        session.add(
            Interaction(
                id=f"interaction-{uuid.uuid4().hex}",
                clinic_id=clinic_id,
                outreach_job_id=job.id,
                channel=channel,
                direction=InteractionDirection.OUTBOUND,
                content=rendered.body,
                outcome=(
                    InteractionOutcome.AUTO_HANDLED
                    if provider_result.successful
                    else InteractionOutcome.IGNORED
                ),
                occurred_at=now,
            )
        )
        _audit_send(session, clinic_id, job, rendered, provider_result)
        session.flush()
        queue_after_commit(
            session,
            "outreach.message.sent",
            {
                "provider": provider_result.provider,
                "channel": channel.value,
                "status": "accepted" if provider_result.successful else "failed",
                "successful": provider_result.successful,
                "message_kind": "booking_confirmation",
            },
        )
        return SendAttemptResult(
            sent=provider_result.successful,
            state=job.state,
            provider_message_id=provider_result.provider_message_id,
            error=provider_result.error,
        )


def _verified_booking_action(
    session: Session,
    outreach_job_id: str,
    *,
    booking_action_id: str | None = None,
) -> BookingAction | None:
    statement = tenant_select(BookingAction).where(
            BookingAction.outreach_job_id == outreach_job_id,
            BookingAction.status == BookingActionStatus.COMPLETED,
            BookingAction.write_back_state == "verified",
            BookingAction.written_back.is_(True),
        )
    if booking_action_id is not None:
        statement = statement.where(BookingAction.id == booking_action_id)
    return session.execute(
        statement.order_by(BookingAction.updated_at.desc(), BookingAction.id.desc()).limit(1)
    ).scalar_one_or_none()


def _has_matching_outbound_interaction(
    session: Session, outreach_job_id: str, channel: Channel, content: str
) -> bool:
    return (
        session.execute(
            select(Interaction.id).where(
                Interaction.outreach_job_id == outreach_job_id,
                Interaction.channel == channel,
                Interaction.direction == InteractionDirection.OUTBOUND,
                Interaction.content == content,
            )
        ).first()
        is not None
    )


def _recipient(patient: Patient, channel: Channel) -> str:
    if channel == Channel.SMS and patient.phone:
        return patient.phone
    if channel == Channel.EMAIL and patient.email:
        return patient.email
    raise ValueError(f"patient {patient.id!r} is not contactable on {channel.value}")


def _send_rendered(
    sender: MessageSender,
    channel: Channel,
    recipient: str,
    rendered: RenderedMessage,
    outreach_job_id: str,
    *,
    status_callback_url: str | None = None,
) -> SendResult:
    if channel == Channel.SMS:
        if status_callback_url is None:
            return sender.send_sms(to=recipient, body=rendered.body, tag=outreach_job_id)
        return sender.send_sms(
            to=recipient,
            body=rendered.body,
            tag=outreach_job_id,
            status_callback_url=status_callback_url,
        )
    return sender.send_email(
        to=recipient,
        subject=rendered.subject or "Appointment follow-up",
        body=rendered.body,
        html_body=rendered.html_body,
    )


def _record_skip(
    session: Session,
    clinic_id: str,
    job: OutreachJob,
    now: datetime,
    skip_reason: SkipReason,
) -> SendAttemptResult:
    retry_at = None
    if skip_reason in PERMANENT_SKIP_REASONS:
        job.state = OutreachState.FAILED
        job.next_action_at = None
    else:
        retry_at = now + TRANSIENT_RETRY_DELAY
        job.next_action_at = retry_at
    audit_action(
        session,
        clinic_id,
        AuditAction.SKIP_CANDIDATE,
        job.id,
        {"channel": job.channel.value, "skip_reason": skip_reason.value},
    )
    session.flush()
    return SendAttemptResult(
        sent=False,
        state=job.state,
        skip_reason=skip_reason,
        retry_at=retry_at,
    )


def _record_size_failure(
    session: Session,
    clinic_id: str,
    job: OutreachJob,
    rendered: RenderedMessage,
) -> SendAttemptResult:
    job.state = OutreachState.FAILED
    job.next_action_at = None
    audit_action(
        session,
        clinic_id,
        AuditAction.SEND_SMS,
        job.id,
        {
            "channel": job.channel.value,
            "template_id": rendered.template_id,
            "message_length": len(rendered.body),
            "successful": False,
            "error": "sms_size_limit",
        },
    )
    session.flush()
    return SendAttemptResult(sent=False, state=job.state, error="sms_size_limit")


def _audit_send(
    session: Session,
    clinic_id: str,
    job: OutreachJob,
    rendered: RenderedMessage,
    provider_result: SendResult,
) -> None:
    action = AuditAction.SEND_SMS if job.channel == Channel.SMS else AuditAction.SEND_EMAIL
    audit_action(
        session,
        clinic_id,
        action,
        job.id,
        {
            "channel": job.channel.value,
            "attempt": job.attempts,
            "state": job.state.value,
            "template_id": rendered.template_id,
            "message_length": len(rendered.body),
            "provider": provider_result.provider,
            "provider_message_id": provider_result.provider_message_id,
            "successful": provider_result.successful,
        },
    )