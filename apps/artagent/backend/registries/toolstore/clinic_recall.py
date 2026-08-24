"""Clinic Recall deterministic ART toolstore hooks."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from apps.artagent.backend.registries.toolstore.registry import register_tool
from src.clinic_recall.availability import get_availability as get_real_availability
from src.clinic_recall.booking import BookingResult
from src.clinic_recall.booking import book_slot as deterministic_book_slot
from src.clinic_recall.booking import reschedule as deterministic_reschedule
from src.clinic_recall.clinic_info import (
    ClinicFaqTopic,
    lookup_sample_clinic_faq,
)
from src.clinic_recall.db import clinic_scope, get_sessionmaker, tenant_select
from src.clinic_recall.durable.config import durable_cliniko_write_enabled
from src.clinic_recall.enums import AuditAction, Channel, EscalationReason
from src.clinic_recall.escalation import escalate_to_staff as deterministic_escalate_to_staff
from src.clinic_recall.identity_evidence import IdentityAction
from src.clinic_recall.identity_runtime import (
    runtime_identity_service,
    trusted_identity_context,
)
from src.clinic_recall.messaging.audit import audit_action
from src.clinic_recall.messaging.opt_out import record_opt_out as deterministic_record_opt_out
from src.clinic_recall.messaging.send import send_email as deterministic_send_email
from src.clinic_recall.messaging.send import send_email_confirmation, send_sms_confirmation
from src.clinic_recall.messaging.send import send_sms as deterministic_send_sms
from src.clinic_recall.messaging.sender import FakeMessageSender
from src.clinic_recall.models import Clinic, OutreachJob, Patient
from src.clinic_recall.pilot_controls import (
    job_gate_for_snapshot,
    operational_switch_snapshot_from_environment,
)
from src.clinic_recall.rights import assert_patient_writable
from src.clinic_recall.types import DEFAULT_TIMEZONE
from utils.ml_logging import get_logger

logger = get_logger("agents.tools.clinic_recall")

TOOL_TAGS = {"clinic_recall", "phase3"}


def _trusted(args: dict[str, Any], key: str) -> str:
    value = str(args.get(f"_{key}") or "").strip()
    if not value:
        raise ValueError(f"missing trusted _{key} tool context")
    return value


def _optional_trusted(args: dict[str, Any], key: str) -> str | None:
    value = str(args.get(f"_{key}") or "").strip()
    return value or None


def _parse_datetime(value: Any, field: str, default_tz: tzinfo | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{field} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if default_tz is None:
            raise ValueError(f"{field} must include a timezone")
        # Interpret a naive wall-clock time in the trusted clinic timezone (never the
        # model's choice), then normalize to UTC for deterministic comparison.
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed.astimezone(UTC)


def _clinic_timezone(session: Any, clinic_id: str) -> tzinfo:
    """Resolve the trusted clinic's timezone for localizing naive tool datetimes."""
    with clinic_scope(session, clinic_id):
        clinic = session.get(Clinic, clinic_id)
    tz_name = clinic.timezone if clinic is not None and clinic.timezone else DEFAULT_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - fall back to the deterministic default timezone
        return ZoneInfo(DEFAULT_TIMEZONE)


def _now(args: dict[str, Any]) -> datetime:
    raw = args.get("now")
    if raw:
        return _parse_datetime(raw, "now")
    return datetime.now(UTC)


def _sender() -> FakeMessageSender:
    return FakeMessageSender()


def _result_error(exc: Exception) -> dict[str, Any]:
    logger.warning("Clinic Recall tool rejected request: %s", exc)
    return {"success": False, "error": str(exc)}


def _reject_model_clinician_filter(args: dict[str, Any]) -> None:
    if str(args.get("clinician_id") or "").strip():
        raise ValueError("clinician_filter_not_allowed")


def _booking_result_payload(result: BookingResult) -> dict[str, Any]:
    return {
        "success": result.success,
        "booking_action_id": result.booking_action_id,
        "status": result.status.value if result.status else None,
        "idempotent": result.idempotent,
        "queued_for_staff": result.queued_for_staff,
        "staff_handoff_created": result.staff_handoff_created,
        "local_action_recorded": result.local_action_recorded,
        "write_back_state": (
            result.write_back_state.value if result.write_back_state else None
        ),
        "provider_confirmed": result.provider_confirmed,
        "error": result.error,
        "message": result.message,
    }


def _booking_result_error(exc: Exception) -> dict[str, Any]:
    return {
        **_result_error(exc),
        "staff_handoff_created": False,
        "local_action_recorded": False,
        "write_back_state": None,
        "provider_confirmed": False,
    }


get_clinic_faq_schema: dict[str, Any] = {
    "name": "get_clinic_faq",
    "description": (
        "Return exact approved administrative facts for the trusted sample clinic. "
        "Select one closed topic; clinic scope is injected by the server. Clinical, "
        "patient-specific, price, live-availability, booking, cross-clinic, and "
        "unsupported questions return a safe status with no fact text."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "enum": [topic.value for topic in ClinicFaqTopic],
                "description": "Closed administrative fact or safe-status topic.",
            },
        },
        "required": ["topic"],
        "additionalProperties": False,
    },
}


async def get_clinic_faq(args: dict[str, Any]) -> dict[str, Any]:
    """Return network-free FAQ facts using only trusted clinic context."""

    try:
        result = lookup_sample_clinic_faq(
            _trusted(args, "clinic_id"),
            str(args.get("topic") or ClinicFaqTopic.UNSUPPORTED.value),
        )
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001 - tool boundary normalizes all failures
        return _result_error(exc)


get_availability_schema: dict[str, Any] = {
    "name": "get_availability",
    "description": "Return real, deterministic appointment slots for the current clinic context.",
    "parameters": {
        "type": "object",
        "properties": {
            "window_start": {"type": "string", "description": "ISO-8601 start of the search window."},
            "window_end": {"type": "string", "description": "ISO-8601 end of the search window."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["window_start", "window_end"],
    },
}


async def get_availability(args: dict[str, Any]) -> dict[str, Any]:
    """Return scoped availability; clinic context is trusted and injected."""
    try:
        clinic_id = _trusted(args, "clinic_id")
        _reject_model_clinician_filter(args)
        patient_id = _optional_trusted(args, "patient_id")
        if patient_id is None:
            return {"success": False, "error": "identity_t2_required"}
        limit = int(args.get("limit") or 5)
        now = _now(args)
        identity_service = runtime_identity_service(now)
        identity_context = trusted_identity_context(args, channel=Channel.CALL)
        with get_sessionmaker()() as session:
            if identity_context is None or not identity_service.authorize(
                session,
                clinic_id=clinic_id,
                evidence_id=identity_context.evidence_id,
                session_id=identity_context.session_id,
                route_id=identity_context.route_id,
                channel=identity_context.channel,
                patient_id=patient_id,
                action=IdentityAction.AVAILABILITY_READ,
            ).allowed:
                return {"success": False, "error": "identity_t2_required"}
            clinic_tz = _clinic_timezone(session, clinic_id)
            window_start = _parse_datetime(args.get("window_start"), "window_start", default_tz=clinic_tz)
            window_end = _parse_datetime(args.get("window_end"), "window_end", default_tz=clinic_tz)
            slots = get_real_availability(
                session,
                clinic_id,
                now=now,
                window_start=window_start,
                window_end=window_end,
                clinician_id=None,
                limit=limit,
            )
        payload: dict[str, Any] = {
            "success": True,
            "slots": [slot.as_dict() for slot in slots],
        }
        if not slots:
            payload["reason"] = "availability_unavailable"
        return payload
    except Exception as exc:  # noqa: BLE001 - tool boundary normalizes all failures
        return _result_error(exc)


book_slot_schema: dict[str, Any] = {
    "name": "book_slot",
    "description": "Create an idempotent deterministic booking action for a previously offered slot.",
    "parameters": {
        "type": "object",
        "properties": {
            "slot_id": {"type": "string", "description": "Slot id returned by get_availability."},
            "require_staff_approval": {"type": "boolean", "description": "Queue for staff approval instead of completing."},
        },
        "required": ["slot_id"],
    },
}


async def book_slot(args: dict[str, Any]) -> dict[str, Any]:
    """Book a slot using only trusted injected patient/job context."""
    try:
        now = _now(args)
        identity_service = runtime_identity_service(now)
        identity_context = trusted_identity_context(args, channel=Channel.CALL)
        with get_sessionmaker()() as session:
            result = deterministic_book_slot(
                session,
                _trusted(args, "clinic_id"),
                patient_id=_trusted(args, "patient_id"),
                outreach_job_id=_trusted(args, "outreach_job_id"),
                slot_id=str(args.get("slot_id") or "").strip(),
                now=now,
                require_staff_approval=bool(args.get("require_staff_approval", False)),
                write_back_enabled=durable_cliniko_write_enabled(),
                identity_service=identity_service,
                identity_context=identity_context,
            )
            session.commit()
        return _booking_result_payload(result)
    except Exception as exc:  # noqa: BLE001
        return _booking_result_error(exc)


reschedule_schema: dict[str, Any] = {
    "name": "reschedule",
    "description": "Create an idempotent deterministic reschedule action for a valid appointment and slot.",
    "parameters": {
        "type": "object",
        "properties": {
            "appointment_id": {"type": "string", "description": "Appointment id being rescheduled."},
            "slot_id": {"type": "string", "description": "Slot id returned by get_availability."},
            "require_staff_approval": {"type": "boolean"},
        },
        "required": ["appointment_id", "slot_id"],
    },
}


async def reschedule(args: dict[str, Any]) -> dict[str, Any]:
    """Reschedule using trusted injected patient/job context."""
    try:
        now = _now(args)
        identity_service = runtime_identity_service(now)
        identity_context = trusted_identity_context(args, channel=Channel.CALL)
        with get_sessionmaker()() as session:
            result = deterministic_reschedule(
                session,
                _trusted(args, "clinic_id"),
                patient_id=_trusted(args, "patient_id"),
                outreach_job_id=_trusted(args, "outreach_job_id"),
                appointment_id=str(args.get("appointment_id") or "").strip(),
                slot_id=str(args.get("slot_id") or "").strip(),
                now=now,
                require_staff_approval=bool(args.get("require_staff_approval", False)),
                write_back_enabled=durable_cliniko_write_enabled(),
                identity_service=identity_service,
                identity_context=identity_context,
            )
            session.commit()
        return _booking_result_payload(result)
    except Exception as exc:  # noqa: BLE001
        return _booking_result_error(exc)


send_sms_schema: dict[str, Any] = {
    "name": "send_sms",
    "description": "Send a deterministic Clinic Recall SMS for the trusted outreach job.",
    "parameters": {
        "type": "object",
        "properties": {
            "template": {
                "type": "string",
                "enum": ["recall", "booking_confirmation"],
                "description": "Deterministic SMS template to send.",
            },
        },
    },
}


async def send_sms(args: dict[str, Any]) -> dict[str, Any]:
    """Send SMS via the deterministic messaging service."""
    try:
        now = _now(args)
        is_confirmation = (
            str(args.get("template") or "recall") == "booking_confirmation"
        )
        with get_sessionmaker()() as session:
            service = (
                send_sms_confirmation
                if is_confirmation
                else deterministic_send_sms
            )
            identity_kwargs = (
                {"identity_service": runtime_identity_service(now)}
                if is_confirmation
                else {}
            )
            result = service(
                session,
                _trusted(args, "clinic_id"),
                _trusted(args, "outreach_job_id"),
                now,
                _sender(),
                pilot_gate=job_gate_for_snapshot(
                    operational_switch_snapshot_from_environment(),
                    Channel.SMS,
                ),
                **identity_kwargs,
            )
            session.commit()
        return {
            "success": result.sent,
            "state": result.state.value,
            "idempotent": result.idempotent,
            "skip_reason": result.skip_reason.value if result.skip_reason else None,
            "pilot_reason": result.pilot_reason,
            "provider_message_id": result.provider_message_id,
            "error": result.error,
        }
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


send_email_schema: dict[str, Any] = {
    "name": "send_email",
    "description": "Send a deterministic Clinic Recall email for the trusted outreach job.",
    "parameters": {
        "type": "object",
        "properties": {
            "template": {
                "type": "string",
                "enum": ["recall", "booking_confirmation"],
                "description": "Deterministic email template to send.",
            },
        },
    },
}


async def send_email(args: dict[str, Any]) -> dict[str, Any]:
    """Send email via the deterministic messaging service."""
    try:
        now = _now(args)
        is_confirmation = (
            str(args.get("template") or "recall") == "booking_confirmation"
        )
        with get_sessionmaker()() as session:
            service = (
                send_email_confirmation
                if is_confirmation
                else deterministic_send_email
            )
            identity_kwargs = (
                {"identity_service": runtime_identity_service(now)}
                if is_confirmation
                else {}
            )
            result = service(
                session,
                _trusted(args, "clinic_id"),
                _trusted(args, "outreach_job_id"),
                now,
                _sender(),
                pilot_gate=job_gate_for_snapshot(
                    operational_switch_snapshot_from_environment(),
                    Channel.SMS,
                ),
                **identity_kwargs,
            )
            session.commit()
        return {
            "success": result.sent,
            "state": result.state.value,
            "idempotent": result.idempotent,
            "skip_reason": result.skip_reason.value if result.skip_reason else None,
            "pilot_reason": result.pilot_reason,
            "provider_message_id": result.provider_message_id,
            "error": result.error,
        }
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


escalate_to_staff_schema: dict[str, Any] = {
    "name": "escalate_to_staff",
    "description": "Create a deterministic staff escalation for clinical, urgent, complaint, or ambiguous signals.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "enum": [reason.value for reason in EscalationReason]},
            "context": {"type": "string", "description": "Short non-clinical reason for staff follow-up."},
        },
        "required": ["reason"],
    },
}


async def escalate_to_staff(args: dict[str, Any]) -> dict[str, Any]:
    """Escalate using trusted injected patient/job context."""
    try:
        with get_sessionmaker()() as session:
            result = deterministic_escalate_to_staff(
                session,
                _trusted(args, "clinic_id"),
                patient_id=_trusted(args, "patient_id"),
                outreach_job_id=_trusted(args, "outreach_job_id"),
                reason=str(args.get("reason") or EscalationReason.AMBIGUOUS.value),
                now=_now(args),
                context=str(args.get("context") or ""),
            )
            session.commit()
        return {
            "success": True,
            "escalation_id": result.escalation_id,
            "interaction_id": result.interaction_id,
            "reason": result.reason.value,
            "priority": result.priority.value,
            "created": result.created,
            "idempotent": result.idempotent,
            "upgraded": result.upgraded,
        }
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


record_opt_out_schema: dict[str, Any] = {
    "name": "record_opt_out",
    "description": "Permanently record an opt-out for the trusted patient context.",
    "parameters": {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "enum": [channel.value for channel in Channel]},
        },
    },
}


async def record_opt_out(args: dict[str, Any]) -> dict[str, Any]:
    """Record an opt-out using trusted injected patient context."""
    try:
        clinic_id = _trusted(args, "clinic_id")
        patient_id = _trusted(args, "patient_id")
        channel = Channel(str(args.get("channel") or Channel.CALL.value))
        with get_sessionmaker()() as session:
            with session.begin():
                with clinic_scope(session, clinic_id):
                    patient = session.get(Patient, patient_id)
                    if patient is None or patient.clinic_id != clinic_id:
                        raise LookupError("patient not found for trusted clinic context")
                    deterministic_record_opt_out(session, clinic_id, patient, channel, _now(args))
        return {"success": True, "channel": channel.value, "patient_id": patient_id}
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


log_outcome_schema: dict[str, Any] = {
    "name": "log_outcome",
    "description": (
        "Record the non-clinical outcome of a Clinic Recall interaction. "
        "Trusted clinic/job context is injected by the server when available."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Opaque session or call correlation identifier.",
            },
            "outcome": {
                "type": "string",
                "enum": ["rebook_requested", "declined", "opt_out", "escalated", "no_answer", "test"],
                "description": "Outcome category to record.",
            },
            "summary": {
                "type": "string",
                "description": "Short non-clinical summary. Do not include diagnosis, symptoms, or medical advice.",
            },
        },
        "required": ["outcome"],
    },
}


async def log_outcome(args: dict[str, Any]) -> dict[str, Any]:
    """Record an outcome; preserve legacy Phase 0 no-context behavior."""
    session_id = str(args.get("session_id") or "").strip()[:128]
    outcome = str(args.get("outcome") or "").strip()
    summary = str(args.get("summary") or "").strip()[:500]

    allowed = {"rebook_requested", "declined", "opt_out", "escalated", "no_answer", "test"}
    if outcome not in allowed:
        return {
            "success": False,
            "message": "Unsupported outcome category.",
            "allowed_outcomes": sorted(allowed),
        }

    event = {
        "session_id": session_id or None,
        "outcome": outcome,
        "summary": summary,
        "recorded_at": datetime.now(UTC).isoformat(),
    }

    clinic_id = _optional_trusted(args, "clinic_id")
    outreach_job_id = _optional_trusted(args, "outreach_job_id")
    if clinic_id and outreach_job_id:
        action = {
            "rebook_requested": AuditAction.BOOK_APPOINTMENT,
            "opt_out": AuditAction.OPT_OUT_PATIENT,
            "escalated": AuditAction.ESCALATE,
        }.get(outcome, AuditAction.PLACE_CALL)
        try:
            with get_sessionmaker()() as session:
                with clinic_scope(session, clinic_id):
                    job = session.execute(
                        tenant_select(OutreachJob).where(OutreachJob.id == outreach_job_id)
                    ).scalar_one_or_none()
                    if job is None:
                        raise LookupError("outreach job not found for trusted clinic context")
                    assert_patient_writable(session, clinic_id, job.patient_id)
                    audit_action(
                        session,
                        clinic_id,
                        action,
                        outreach_job_id,
                        {"outcome": outcome, "summary": summary, "session_id": session_id},
                        actor="system:recall-agent",
                    )
                session.commit()
        except Exception as exc:  # noqa: BLE001
            return _result_error(exc)

    logger.info("Clinic Recall outcome recorded: %s", event)

    return {
        "success": True,
        "logged": True,
        "event": event,
        "message": "Outcome logged for Phase 0 proof.",
    }


for schema, executor in (
    (get_clinic_faq_schema, get_clinic_faq),
    (get_availability_schema, get_availability),
    (book_slot_schema, book_slot),
    (reschedule_schema, reschedule),
    (send_sms_schema, send_sms),
    (send_email_schema, send_email),
    (escalate_to_staff_schema, escalate_to_staff),
    (record_opt_out_schema, record_opt_out),
    (log_outcome_schema, log_outcome),
):
    register_tool(schema["name"], schema, executor, tags=TOOL_TAGS)


__all__ = [
    "book_slot",
    "book_slot_schema",
    "escalate_to_staff",
    "escalate_to_staff_schema",
    "get_clinic_faq",
    "get_clinic_faq_schema",
    "get_availability",
    "get_availability_schema",
    "log_outcome",
    "log_outcome_schema",
    "record_opt_out",
    "record_opt_out_schema",
    "reschedule",
    "reschedule_schema",
    "send_email",
    "send_email_schema",
    "send_sms",
    "send_sms_schema",
]