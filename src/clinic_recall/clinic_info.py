"""Patient-safe clinic logistics for inbound calls."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from .db import clinic_scope
from .models import Clinic

SAMPLE_CLINIC_FAQ_ID = "clinic-voice-demo"
SAMPLE_CLINIC_FAQ_SOURCE_ID = "clinic-voice-demo-faq-v1"


class ClinicFaqTopic(StrEnum):
    DISPLAY_NAME = "display_name"
    LOCATION = "location"
    OPENING_HOURS = "opening_hours"
    PARKING = "parking"
    PUBLIC_TRANSPORT = "public_transport"
    ACCESSIBILITY = "accessibility"
    ARRIVAL = "arrival"
    CONTACT = "contact"
    CANCELLATION_NOTICE = "cancellation_notice"
    ADMINISTRATIVE_SERVICES = "administrative_services"
    CLINICAL = "clinical"
    PATIENT_SPECIFIC = "patient_specific"
    PRICE = "price"
    LIVE_AVAILABILITY = "live_availability"
    BOOKING = "booking"
    CROSS_CLINIC = "cross_clinic"
    UNSUPPORTED = "unsupported"


SUPPORTED_CLINIC_FAQ_TOPICS = frozenset(
    {
        ClinicFaqTopic.DISPLAY_NAME,
        ClinicFaqTopic.LOCATION,
        ClinicFaqTopic.OPENING_HOURS,
        ClinicFaqTopic.PARKING,
        ClinicFaqTopic.PUBLIC_TRANSPORT,
        ClinicFaqTopic.ACCESSIBILITY,
        ClinicFaqTopic.ARRIVAL,
        ClinicFaqTopic.CONTACT,
        ClinicFaqTopic.CANCELLATION_NOTICE,
        ClinicFaqTopic.ADMINISTRATIVE_SERVICES,
    }
)

_SAMPLE_CLINIC_FAQ_FACTS: dict[
    ClinicFaqTopic,
    tuple[tuple[str, str | list[str]], ...],
] = {
    ClinicFaqTopic.DISPLAY_NAME: (("display_name", "Northstar Therapy Demo Clinic"),),
    ClinicFaqTopic.LOCATION: (("location", "Example House, 1 Demo Way, Sampletown, EX0 0PL"),),
    ClinicFaqTopic.OPENING_HOURS: (
        ("hours_weekday", "Monday to Friday, 08:30 to 18:00"),
        ("hours_saturday", "Saturday, 09:00 to 13:00"),
        ("hours_sunday", "Sunday, closed"),
    ),
    ClinicFaqTopic.PARKING: (
        ("parking", "Free visitor parking is available in the marked demo-clinic bays."),
    ),
    ClinicFaqTopic.PUBLIC_TRANSPORT: (
        (
            "public_transport",
            "The fictional Sampletown Central stop is a five-minute level walk away.",
        ),
    ),
    ClinicFaqTopic.ACCESSIBILITY: (
        ("step_free_access", "The entrance is step-free."),
        ("accessible_facilities", "An accessible toilet and lift access are available."),
    ),
    ClinicFaqTopic.ARRIVAL: (
        (
            "arrival_guidance",
            "Please arrive 10 minutes before your appointment and check in at reception.",
        ),
    ),
    ClinicFaqTopic.CONTACT: (
        ("contact_phone", "+44 1632 960 000"),
        ("contact_email", "appointments@example.test"),
        ("contact_website", "https://clinic-voice-demo.example"),
    ),
    ClinicFaqTopic.CANCELLATION_NOTICE: (
        ("cancellation_notice", "Please give at least 24 hours' notice when possible."),
    ),
    ClinicFaqTopic.ADMINISTRATIVE_SERVICES: (
        (
            "administrative_services",
            [
                "appointment booking requests",
                "appointment rescheduling requests",
                "appointment reminders",
                "non-clinical callback requests",
            ],
        ),
    ),
}

_SAMPLE_CLINIC_FAQ_ANSWERS = {
    ClinicFaqTopic.DISPLAY_NAME: "The clinic is Northstar Therapy Demo Clinic.",
    ClinicFaqTopic.LOCATION: ("The clinic is at Example House, 1 Demo Way, Sampletown, EX0 0PL."),
    ClinicFaqTopic.OPENING_HOURS: (
        "The clinic is open Monday to Friday from 08:30 to 18:00, Saturday from "
        "09:00 to 13:00, and is closed on Sunday."
    ),
    ClinicFaqTopic.PARKING: ("Free visitor parking is available in the marked demo-clinic bays."),
    ClinicFaqTopic.PUBLIC_TRANSPORT: (
        "The fictional Sampletown Central stop is a five-minute level walk away."
    ),
    ClinicFaqTopic.ACCESSIBILITY: (
        "The entrance is step-free, and an accessible toilet and lift access are available."
    ),
    ClinicFaqTopic.ARRIVAL: (
        "Please arrive 10 minutes before your appointment and check in at reception."
    ),
    ClinicFaqTopic.CONTACT: (
        "You can call +44 1632 960 000, email appointments@example.test, or visit "
        "clinic-voice-demo.example."
    ),
    ClinicFaqTopic.CANCELLATION_NOTICE: (
        "Please give at least 24 hours' cancellation notice when possible."
    ),
    ClinicFaqTopic.ADMINISTRATIVE_SERVICES: (
        "Reception can help with appointment booking requests, appointment rescheduling "
        "requests, appointment reminders, and non-clinical callback requests."
    ),
}

_FAQ_UNAVAILABLE_ANSWER = (
    "I can't confirm that from the clinic's approved information, so the clinic team "
    "will need to help."
)

_BLOCKED_REASON_CODES = {
    ClinicFaqTopic.CLINICAL: "clinical_safety_route_required",
    ClinicFaqTopic.PATIENT_SPECIFIC: "patient_specific_request_not_supported",
    ClinicFaqTopic.PRICE: "price_not_approved",
    ClinicFaqTopic.LIVE_AVAILABILITY: "live_availability_tool_required",
    ClinicFaqTopic.BOOKING: "booking_tool_required",
    ClinicFaqTopic.CROSS_CLINIC: "cross_clinic_request_rejected",
    ClinicFaqTopic.UNSUPPORTED: "unsupported_fact",
}


def classify_clinic_faq_topic(query: str) -> ClinicFaqTopic:
    """Classify a bounded clinic-logistics question without selecting tenant scope."""

    text = " ".join(re.sub(r"[^a-z0-9\s'-]", " ", query.lower()).split())
    if not text:
        return ClinicFaqTopic.UNSUPPORTED
    if any(
        phrase in text
        for phrase in (
            "ignore instructions",
            "ignore your instructions",
            "system prompt",
            "developer message",
            "pretend to be staff",
        )
    ):
        return ClinicFaqTopic.UNSUPPORTED
    if any(
        phrase in text
        for phrase in (
            "another clinic",
            "different clinic",
            "other clinic",
            "switch clinic",
        )
    ):
        return ClinicFaqTopic.CROSS_CLINIC
    if any(
        phrase in text
        for phrase in (
            "my appointment",
            "my booking",
            "my clinician",
            "my record",
            "my results",
            "patient record",
        )
    ):
        return ClinicFaqTopic.PATIENT_SPECIFIC
    if any(
        term in text.split()
        for term in (
            "diagnose",
            "diagnosis",
            "medicine",
            "pain",
            "symptom",
            "symptoms",
            "treatment",
        )
    ) or any(phrase in text for phrase in ("medical advice", "what should i take")):
        return ClinicFaqTopic.CLINICAL
    if any(
        phrase in text
        for phrase in (
            "cancellation notice",
            "cancellation policy",
            "notice to cancel",
            "notice do you need",
        )
    ):
        return ClinicFaqTopic.CANCELLATION_NOTICE
    if (
        any(term in text.split() for term in ("cost", "costs", "fee", "fees", "price", "prices"))
        or "how much" in text
    ):
        return ClinicFaqTopic.PRICE
    if (
        any(
            phrase in text
            for phrase in (
                "available appointment",
                "available appointments",
                "appointment slot",
                "appointment slots",
                "available slot",
                "available slots",
                "clinician schedule",
                "live availability",
                "next appointment",
            )
        )
        or "availability" in text
    ):
        return ClinicFaqTopic.LIVE_AVAILABILITY
    if any(
        phrase in text
        for phrase in (
            "book me",
            "book an appointment",
            "cancel my appointment",
            "change my appointment",
            "move my appointment",
            "reschedule my appointment",
        )
    ):
        return ClinicFaqTopic.BOOKING
    if any(
        phrase in text
        for phrase in ("clinic name", "name of the clinic", "calling from", "who is calling")
    ):
        return ClinicFaqTopic.DISPLAY_NAME
    if any(term in text.split() for term in ("contact", "email", "phone", "telephone", "website")):
        return ClinicFaqTopic.CONTACT
    if (
        any(term in text.split() for term in ("address", "located", "location"))
        or "where are you" in text
    ):
        return ClinicFaqTopic.LOCATION
    if any(
        term in text.split() for term in ("hours", "open", "opening", "close", "closing", "weekend")
    ):
        return ClinicFaqTopic.OPENING_HOURS
    if "parking" in text or "car park" in text:
        return ClinicFaqTopic.PARKING
    if any(phrase in text for phrase in ("public transport", "nearest stop")) or any(
        term in text.split() for term in ("bus", "train")
    ):
        return ClinicFaqTopic.PUBLIC_TRANSPORT
    if any(
        term in text.split() for term in ("accessible", "accessibility", "lift", "wheelchair")
    ) or any(phrase in text for phrase in ("step free", "step-free", "accessible toilet")):
        return ClinicFaqTopic.ACCESSIBILITY
    if any(term in text.split() for term in ("arrive", "arrival")) or "check in" in text:
        return ClinicFaqTopic.ARRIVAL
    if any(
        phrase in text
        for phrase in (
            "administrative services",
            "admin services",
            "appointment reminders",
            "callback requests",
            "what can reception help with",
        )
    ):
        return ClinicFaqTopic.ADMINISTRATIVE_SERVICES
    return ClinicFaqTopic.UNSUPPORTED


def lookup_sample_clinic_faq(
    clinic_id: str,
    topic: ClinicFaqTopic | str,
) -> dict[str, Any]:
    """Return exact approved facts for the trusted synthetic clinic fixture."""

    try:
        resolved_topic = ClinicFaqTopic(str(topic))
    except ValueError:
        resolved_topic = ClinicFaqTopic.UNSUPPORTED
    if clinic_id != SAMPLE_CLINIC_FAQ_ID:
        return {
            "status": "not_available",
            "topic": resolved_topic.value,
            "reason_code": "trusted_clinic_not_configured",
            "source_id": None,
            "facts": [],
        }
    if resolved_topic not in SUPPORTED_CLINIC_FAQ_TOPICS:
        return {
            "status": "not_supported",
            "topic": resolved_topic.value,
            "reason_code": _BLOCKED_REASON_CODES[resolved_topic],
            "source_id": SAMPLE_CLINIC_FAQ_SOURCE_ID,
            "facts": [],
        }
    return {
        "status": "answered",
        "topic": resolved_topic.value,
        "reason_code": None,
        "source_id": SAMPLE_CLINIC_FAQ_SOURCE_ID,
        "facts": [
            {"fact_id": fact_id, "value": value}
            for fact_id, value in _SAMPLE_CLINIC_FAQ_FACTS[resolved_topic]
        ],
    }


def format_sample_clinic_faq_answer(result: dict[str, Any]) -> str:
    """Render only an exact answer whose source and facts match the fixture."""

    try:
        topic = ClinicFaqTopic(str(result.get("topic") or ""))
    except ValueError:
        return _FAQ_UNAVAILABLE_ANSWER
    if topic not in SUPPORTED_CLINIC_FAQ_TOPICS:
        return _FAQ_UNAVAILABLE_ANSWER
    approved = lookup_sample_clinic_faq(SAMPLE_CLINIC_FAQ_ID, topic)
    if any(
        result.get(key) != approved[key]
        for key in ("status", "topic", "reason_code", "source_id", "facts")
    ):
        return _FAQ_UNAVAILABLE_ANSWER
    return _SAMPLE_CLINIC_FAQ_ANSWERS[topic]


def get_clinic_hours(session: Session, clinic_id: str) -> dict[str, Any]:
    """Return deterministic, patient-safe opening/contact hours."""
    with clinic_scope(session, clinic_id):
        clinic = session.get(Clinic, clinic_id)
        if clinic is None:
            raise LookupError("clinic not found")
        return {
            "clinic_id": clinic.id,
            "timezone": clinic.timezone,
            "contact_hours": clinic.contact_hours or {},
        }


def get_clinic_services(session: Session, clinic_id: str) -> dict[str, Any]:
    """Return configured non-clinical service labels, if present."""
    with clinic_scope(session, clinic_id):
        clinic = session.get(Clinic, clinic_id)
        if clinic is None:
            raise LookupError("clinic not found")
        branding = clinic.branding or {}
        policy = clinic.consent_policy or {}
        services = branding.get("services") or policy.get("services") or []
        if not isinstance(services, list):
            services = []
        return {"clinic_id": clinic.id, "services": [str(service) for service in services]}
