"""Scoped outbox and interaction read models for staff review surfaces."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .candidate_queue import _patient_view, clinic_config_from_row
from .db import clinic_scope, tenant_select
from .eligibility import evaluate
from .enums import CampaignStatus, CampaignType, Channel, InteractionDirection, OutreachState
from .messaging.history import contact_history_for_send
from .messaging.templates import (
    RenderedMessage,
    render_feedback_email,
    render_feedback_sms,
    render_recall_email,
    render_recall_sms,
)
from .models import Appointment, Campaign, Clinic, Interaction, OutreachJob, Patient
from .pilot_controls import PatientPilotGate


class OutboxItem(BaseModel):
    """One queued deterministic outbound item awaiting campaign review."""

    model_config = ConfigDict(from_attributes=True)

    item_id: str
    outreach_job_id: str
    campaign_id: str
    patient_name: str
    reason_code: str | None
    channel: str
    template_id: str | None
    message_preview: str | None
    scheduled_for: datetime
    campaign_status: str
    eligible_now: bool
    skip_reason: str | None
    can_send_after_approval: bool


class InteractionTimelineItem(BaseModel):
    """Minimal interaction history for staff timelines."""

    model_config = ConfigDict(from_attributes=True)

    item_id: str
    channel: str
    direction: str
    occurred_at: datetime
    intent: str | None
    outcome: str | None
    outreach_job_id: str
    template_id: str | None
    content_preview: str | None


def list_outbox_items(
    session: Session,
    clinic_id: str,
    now: datetime,
    *,
    limit: int = 100,
    pilot_gate: PatientPilotGate,
) -> list[OutboxItem]:
    """List queued jobs for a clinic with deterministic message previews."""
    _require_aware("now", now)
    bounded_limit = max(1, min(limit, 250))
    with clinic_scope(session, clinic_id):
        clinic = _load_clinic(session, clinic_id)
        if clinic is None:
            raise LookupError(f"clinic {clinic_id!r} not found")
        jobs = list(
            session.execute(
                tenant_select(OutreachJob)
                .where(OutreachJob.state == OutreachState.QUEUED)
                .order_by(OutreachJob.created_at, OutreachJob.id)
                .limit(bounded_limit)
            ).scalars()
        )
        return [
            _outbox_item(session, clinic, job, now, pilot_gate=pilot_gate)
            for job in jobs
        ]


def list_interaction_timeline(
    session: Session,
    clinic_id: str,
    *,
    limit: int = 100,
) -> list[InteractionTimelineItem]:
    """List scoped interaction metadata without broadly exposing raw text."""
    bounded_limit = max(1, min(limit, 250))
    with clinic_scope(session, clinic_id):
        clinic = _load_clinic(session, clinic_id)
        if clinic is None:
            raise LookupError(f"clinic {clinic_id!r} not found")
        interactions = list(
            session.execute(
                tenant_select(Interaction)
                .order_by(Interaction.occurred_at.desc(), Interaction.id.desc())
                .limit(bounded_limit)
            ).scalars()
        )
        return [_timeline_item(session, clinic, interaction) for interaction in interactions]


def _outbox_item(
    session: Session,
    clinic: Clinic,
    job: OutreachJob,
    now: datetime,
    *,
    pilot_gate: PatientPilotGate,
) -> OutboxItem:
    campaign = _load_campaign(session, job.campaign_id)
    patient = _load_patient(session, job.patient_id)
    appointment = _load_appointment(session, job.appointment_id)
    if campaign is None or patient is None:
        raise LookupError("outbox job references missing scoped rows")

    config = clinic_config_from_row(clinic)
    history = contact_history_for_send(session, clinic.id, patient.id, now, config)
    decision = evaluate(_patient_view(patient), config, history, now, job.channel)
    pilot_channel = Channel.SMS if job.channel == Channel.EMAIL else job.channel
    pilot_decision = pilot_gate(
        session,
        clinic.id,
        patient.id,
        pilot_channel,
        now,
    )
    eligible_now = decision.eligible and pilot_decision.allowed
    skip_reason = (
        pilot_decision.reason
        if not pilot_decision.allowed
        else decision.skip_reason.value
        if decision.skip_reason
        else None
    )
    rendered = _render_for_job(job.channel, campaign.type, clinic, patient, appointment)
    return OutboxItem(
        item_id=f"outbox:{job.id}",
        outreach_job_id=job.id,
        campaign_id=campaign.id,
        patient_name=_minimized_name(patient.name),
        reason_code=job.reason_code.value if job.reason_code else None,
        channel=job.channel.value,
        template_id=rendered.template_id if rendered else None,
        message_preview=_preview(rendered.body) if rendered else None,
        scheduled_for=_as_utc(job.next_action_at or job.created_at),
        campaign_status=campaign.status.value,
        eligible_now=eligible_now,
        skip_reason=skip_reason,
        can_send_after_approval=(
            eligible_now
            and rendered is not None
            and campaign.status
            in {CampaignStatus.DRAFT, CampaignStatus.PAUSED, CampaignStatus.ACTIVE}
        ),
    )


def _timeline_item(
    session: Session,
    clinic: Clinic,
    interaction: Interaction,
) -> InteractionTimelineItem:
    template_id: str | None = None
    content_preview: str | None = None
    job = _load_job(session, interaction.outreach_job_id)
    if job is not None and interaction.direction == InteractionDirection.OUTBOUND:
        campaign = _load_campaign(session, job.campaign_id)
        patient = _load_patient(session, job.patient_id)
        appointment = _load_appointment(session, job.appointment_id)
        rendered = (
            _render_for_job(job.channel, campaign.type, clinic, patient, appointment)
            if campaign is not None and patient is not None
            else None
        )
        if rendered is not None and interaction.content == rendered.body:
            template_id = rendered.template_id
            content_preview = _preview(rendered.body)
    return InteractionTimelineItem(
        item_id=f"interaction:{interaction.id}",
        channel=interaction.channel.value,
        direction=interaction.direction.value,
        occurred_at=_as_utc(interaction.occurred_at),
        intent=interaction.intent.value if interaction.intent else None,
        outcome=interaction.outcome.value if interaction.outcome else None,
        outreach_job_id=interaction.outreach_job_id,
        template_id=template_id,
        content_preview=content_preview,
    )


def _render_for_job(
    channel: Channel,
    campaign_type: CampaignType,
    clinic: Clinic,
    patient: Patient,
    appointment: Appointment | None,
) -> RenderedMessage | None:
    if campaign_type == CampaignType.FEEDBACK:
        if channel == Channel.SMS:
            return render_feedback_sms(clinic, patient, appointment)
        if channel == Channel.EMAIL:
            return render_feedback_email(clinic, patient, appointment)
    if channel == Channel.SMS:
        return render_recall_sms(clinic, patient, appointment)
    if channel == Channel.EMAIL:
        return render_recall_email(clinic, patient, appointment)
    return None


def _load_clinic(session: Session, clinic_id: str) -> Clinic | None:
    return session.execute(tenant_select(Clinic).where(Clinic.id == clinic_id)).scalar_one_or_none()


def _load_campaign(session: Session, campaign_id: str | None) -> Campaign | None:
    if not campaign_id:
        return None
    return session.execute(tenant_select(Campaign).where(Campaign.id == campaign_id)).scalar_one_or_none()


def _load_patient(session: Session, patient_id: str | None) -> Patient | None:
    if not patient_id:
        return None
    return session.execute(tenant_select(Patient).where(Patient.id == patient_id)).scalar_one_or_none()


def _load_appointment(session: Session, appointment_id: str | None) -> Appointment | None:
    if not appointment_id:
        return None
    return session.execute(
        tenant_select(Appointment).where(Appointment.id == appointment_id)
    ).scalar_one_or_none()


def _load_job(session: Session, outreach_job_id: str | None) -> OutreachJob | None:
    if not outreach_job_id:
        return None
    return session.execute(
        tenant_select(OutreachJob).where(OutreachJob.id == outreach_job_id)
    ).scalar_one_or_none()


def _minimized_name(name: str) -> str:
    parts = name.strip().split()
    if not parts:
        return "Patient"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}."


def _preview(body: str, max_length: int = 220) -> str:
    collapsed = " ".join(body.split())
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[: max_length - 1].rstrip() + "..."


def _require_aware(field: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)