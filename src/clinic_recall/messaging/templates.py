"""Clinic-branded deterministic outreach templates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ..models import Appointment, Clinic, Patient


@dataclass(frozen=True)
class RenderedMessage:
    """A rendered outbound message body and metadata safe for audit."""

    template_id: str
    body: str
    subject: str | None = None
    html_body: str | None = None


def _first_name(name: str) -> str:
    return (name.strip().split() or ["there"])[0]


def _booking_url(clinic: Clinic) -> str | None:
    branding = clinic.branding or {}
    value = branding.get("booking_url") or branding.get("reschedule_url")
    return str(value) if value else None


def _appointment_date(appointment: Appointment | None) -> str:
    if appointment is None:
        return "your recent appointment"
    start_at = appointment.start_at
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=UTC)
    return start_at.strftime("%d %b %Y")


def _slot_time(start_at: datetime | None) -> str:
    if start_at is None:
        return "the arranged time"
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=UTC)
    return start_at.strftime("%d %b %Y at %H:%M")


def render_recall_sms(
    clinic: Clinic,
    patient: Patient,
    appointment: Appointment | None,
) -> RenderedMessage:
    """Render the SMS-first Clinic Recall message."""
    url = _booking_url(clinic)
    action = f"To rebook, visit {url} or reply YES." if url else "Reply YES to rebook."
    body = (
        f"Hi {_first_name(patient.name)}, this is {clinic.name}. "
        f"We missed you for {_appointment_date(appointment)}. "
        f"{action}\n\nReply STOP to opt out."
    )
    return RenderedMessage(template_id="clinic_recall_sms_v1", body=body)


def render_recall_email(
    clinic: Clinic,
    patient: Patient,
    appointment: Appointment | None,
) -> RenderedMessage:
    """Render the email follow-up Clinic Recall message."""
    url = _booking_url(clinic)
    action = f"You can rebook here: {url}" if url else "Reply to this email to rebook."
    body = (
        f"Hi {_first_name(patient.name)},\n\n"
        f"This is {clinic.name}. We missed you for {_appointment_date(appointment)}.\n\n"
        f"{action}\n\n"
        "If you no longer want email reminders, reply STOP."
    )
    return RenderedMessage(
        template_id="clinic_recall_email_v1",
        subject=f"Rebook your appointment with {clinic.name}",
        body=body,
    )


def render_feedback_sms(
    clinic: Clinic,
    patient: Patient,
    appointment: Appointment | None,
) -> RenderedMessage:
    """Render a short post-visit feedback request."""
    body = (
        f"Hi {_first_name(patient.name)}, this is {clinic.name}. "
        f"Please rate your visit on {_appointment_date(appointment)} from 1-5 "
        "and add a short comment if you wish. Reply STOP to opt out."
    )
    return RenderedMessage(template_id="clinic_feedback_sms_v1", body=body)


def render_feedback_email(
    clinic: Clinic,
    patient: Patient,
    appointment: Appointment | None,
) -> RenderedMessage:
    """Render a post-visit feedback email request."""
    body = (
        f"Hi {_first_name(patient.name)},\n\n"
        f"This is {clinic.name}. Please rate your visit on {_appointment_date(appointment)} "
        "from 1-5 and add a short comment if you wish.\n\n"
        "If you no longer want email reminders, reply STOP."
    )
    return RenderedMessage(
        template_id="clinic_feedback_email_v1",
        subject=f"How was your visit with {clinic.name}?",
        body=body,
    )


def render_booking_confirmation_sms(
    clinic: Clinic,
    patient: Patient,
    slot_start_at: datetime | None,
) -> RenderedMessage:
    """Render a deterministic SMS confirmation after a successful booking action."""
    body = (
        f"Hi {_first_name(patient.name)}, this is {clinic.name}. "
        f"Your appointment is booked for {_slot_time(slot_start_at)}. "
        "Please contact the clinic if you need to change it. Reply STOP to opt out."
    )
    return RenderedMessage(template_id="clinic_booking_confirmation_sms_v1", body=body)


def render_booking_confirmation_email(
    clinic: Clinic,
    patient: Patient,
    slot_start_at: datetime | None,
) -> RenderedMessage:
    """Render a deterministic email confirmation after a successful booking action."""
    body = (
        f"Hi {_first_name(patient.name)},\n\n"
        f"Your appointment with {clinic.name} is booked for {_slot_time(slot_start_at)}.\n\n"
        "Please contact the clinic if you need to change it."
    )
    return RenderedMessage(
        template_id="clinic_booking_confirmation_email_v1",
        subject=f"Appointment confirmation from {clinic.name}",
        body=body,
    )