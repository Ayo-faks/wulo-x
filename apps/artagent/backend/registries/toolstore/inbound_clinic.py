"""Deterministic inbound clinic ART toolstore hooks."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from apps.artagent.backend.registries.toolstore.registry import register_tool
from src.clinic_recall.availability import get_availability as get_real_availability
from src.clinic_recall.clinic_info import get_clinic_hours as deterministic_get_clinic_hours
from src.clinic_recall.clinic_info import get_clinic_services as deterministic_get_clinic_services
from src.clinic_recall.db import clinic_scope, get_sessionmaker
from src.clinic_recall.enums import Channel, InboundStaffTaskKind
from src.clinic_recall.identity_evidence import IdentityAction
from src.clinic_recall.identity_runtime import (
    runtime_identity_service,
    trusted_identity_context,
)
from src.clinic_recall.inbound_calls import (
    log_inbound_call_outcome as deterministic_log_inbound_call_outcome,
)
from src.clinic_recall.inbound_calls import (
    record_consent_decision as deterministic_record_consent_decision,
)
from src.clinic_recall.inbound_identity import resolve_single_inbound_patient_id
from src.clinic_recall.inbound_staff_tasks import create_inbound_staff_task
from src.clinic_recall.messaging.opt_out import record_opt_out as deterministic_record_opt_out
from src.clinic_recall.models import Clinic, Patient
from src.clinic_recall.types import DEFAULT_TIMEZONE
from utils.ml_logging import get_logger

logger = get_logger("agents.tools.inbound_clinic")

TOOL_TAGS = {"clinic_recall", "inbound"}
TERMINAL_ESCALATION_REASONS = {"urgent", "clinical", "complaint", "safeguarding", "distress"}
INBOUND_ESCALATION_REASONS = TERMINAL_ESCALATION_REASONS | {
    "ambiguous",
    "identity_policy_unavailable",
}
INBOUND_OUTCOME_CODES = (
    "callback_requested",
    "clinic_info",
    "completed",
    "opt_out",
    "staff_handoff",
)


def _trusted(args: dict[str, Any], key: str) -> str:
    value = str(args.get(f"_{key}") or "").strip()
    if not value:
        raise ValueError(f"missing trusted _{key} tool context")
    return value


def _optional_trusted(args: dict[str, Any], key: str) -> str | None:
    value = str(args.get(f"_{key}") or "").strip()
    return value or None


def _require_inbound(args: dict[str, Any]) -> None:
    direction = _trusted(args, "call_direction")
    if direction != "inbound":
        raise ValueError("tool requires trusted inbound call context")


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
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed.astimezone(UTC)


def _now(args: dict[str, Any]) -> datetime:
    raw = args.get("now")
    if raw:
        return _parse_datetime(raw, "now")
    return datetime.now(UTC)


def _clinic_timezone(session: Any, clinic_id: str) -> tzinfo:
    with clinic_scope(session, clinic_id):
        clinic = session.get(Clinic, clinic_id)
    tz_name = clinic.timezone if clinic is not None and clinic.timezone else DEFAULT_TIMEZONE
    try:
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo(DEFAULT_TIMEZONE)


def _result_error(exc: Exception) -> dict[str, Any]:
    logger.warning("Inbound clinic tool rejected request: %s", exc)
    return {"success": False, "error": str(exc)}


def _reject_model_clinician_filter(args: dict[str, Any]) -> None:
    if str(args.get("clinician_id") or "").strip():
        raise ValueError("clinician_filter_not_allowed")


get_clinic_hours_schema: dict[str, Any] = {
    "name": "get_clinic_hours",
    "description": "Return deterministic opening/contact hours for the trusted inbound clinic.",
    "parameters": {"type": "object", "properties": {}},
}


async def get_clinic_hours(args: dict[str, Any]) -> dict[str, Any]:
    try:
        _require_inbound(args)
        with get_sessionmaker()() as session:
            hours = deterministic_get_clinic_hours(session, _trusted(args, "clinic_id"))
        return {"success": True, **hours}
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


get_clinic_services_schema: dict[str, Any] = {
    "name": "get_clinic_services",
    "description": "Return configured non-clinical service labels for the trusted inbound clinic.",
    "parameters": {"type": "object", "properties": {}},
}


async def get_clinic_services(args: dict[str, Any]) -> dict[str, Any]:
    try:
        _require_inbound(args)
        with get_sessionmaker()() as session:
            services = deterministic_get_clinic_services(session, _trusted(args, "clinic_id"))
        return {"success": True, **services}
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


find_possible_patient_match_schema: dict[str, Any] = {
    "name": "find_possible_patient_match",
    "description": "Request generic staff identity verification without revealing patient existence.",
    "parameters": {"type": "object", "properties": {}},
}


async def find_possible_patient_match(args: dict[str, Any]) -> dict[str, Any]:
    try:
        _require_inbound(args)
        _trusted(args, "clinic_id")
        return {
            "success": True,
            "status": "staff_verification_required",
        }
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


record_inbound_opt_out_schema: dict[str, Any] = {
    "name": "record_inbound_opt_out",
    "description": "Record call opt-out from trusted inbound route context or create staff review.",
    "parameters": {"type": "object", "properties": {}},
}


async def record_inbound_opt_out(args: dict[str, Any]) -> dict[str, Any]:
    try:
        _require_inbound(args)
        clinic_id = _trusted(args, "clinic_id")
        now = _now(args)
        with get_sessionmaker()() as session, session.begin():
            match = resolve_single_inbound_patient_id(
                session,
                clinic_id,
                _optional_trusted(args, "caller_number_hash"),
            )
            if match.status == "single_match" and match.patient_id:
                with clinic_scope(session, clinic_id):
                    patient = session.get(Patient, match.patient_id)
                if patient is None or patient.clinic_id != clinic_id:
                    raise LookupError("patient not found for trusted inbound context")
                deterministic_record_opt_out(
                    session,
                    clinic_id,
                    patient,
                    Channel.CALL,
                    now,
                )
                return {
                    "success": True,
                    "status": "recorded",
                    "identity_review_created": False,
                }
            create_inbound_staff_task(
                session,
                clinic_id,
                inbound_call_id=_trusted(args, "inbound_call_id"),
                kind=InboundStaffTaskKind.IDENTITY_UNCLEAR,
                priority="high",
                reason="opt_out_identity_review",
                summary="Inbound call opt-out requires staff identity review.",
                payload={},
                now=now,
            )
            return {
                "success": True,
                "status": "staff_review_required",
                "identity_review_created": True,
            }
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


get_available_slots_schema: dict[str, Any] = {
    "name": "get_available_slots",
    "description": "Return real deterministic appointment slots for the trusted inbound clinic.",
    "parameters": {
        "type": "object",
        "properties": {
            "window_start": {"type": "string"},
            "window_end": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["window_start", "window_end"],
    },
}


async def get_available_slots(args: dict[str, Any]) -> dict[str, Any]:
    try:
        _require_inbound(args)
        _reject_model_clinician_filter(args)
        clinic_id = _trusted(args, "clinic_id")
        patient_id = _optional_trusted(args, "patient_id")
        now = _now(args)
        identity_service = runtime_identity_service(now)
        identity_context = trusted_identity_context(args, channel=Channel.CALL)
        if patient_id is None or identity_context is None:
            return {"success": False, "error": "identity_t2_required", "slots": []}
        with get_sessionmaker()() as session:
            if not identity_service.authorize(
                session,
                clinic_id=clinic_id,
                evidence_id=identity_context.evidence_id,
                session_id=identity_context.session_id,
                route_id=identity_context.route_id,
                channel=identity_context.channel,
                patient_id=patient_id,
                action=IdentityAction.AVAILABILITY_READ,
            ).allowed:
                return {"success": False, "error": "identity_t2_required", "slots": []}
            clinic_tz = _clinic_timezone(session, clinic_id)
            slots = get_real_availability(
                session,
                clinic_id,
                now=now,
                window_start=_parse_datetime(args.get("window_start"), "window_start", clinic_tz),
                window_end=_parse_datetime(args.get("window_end"), "window_end", clinic_tz),
                clinician_id=None,
                limit=int(args.get("limit") or 5),
            )
        payload: dict[str, Any] = {
            "success": True,
            "timezone": str(clinic_tz),
            "slots": [slot.as_dict() for slot in slots],
        }
        if not slots:
            payload["reason"] = "availability_unavailable"
        return payload
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


create_inbound_booking_request_schema: dict[str, Any] = {
    "name": "create_inbound_booking_request",
    "description": "Create a staff-owned inbound booking request or soft hold; never confirms a completed booking.",
    "parameters": {
        "type": "object",
        "properties": {
            "slot_id": {"type": "string"},
            "requested_service": {"type": "string"},
            "requested_time": {"type": "string"},
            "summary": {"type": "string"},
        },
    },
}


async def create_inbound_booking_request(args: dict[str, Any]) -> dict[str, Any]:
    generic_args = {
        key: value
        for key, value in args.items()
        if key.startswith("_") or key == "now"
    }
    generic_args["summary"] = "Generic booking request requires staff review."
    return await _create_task_tool(
        generic_args,
        InboundStaffTaskKind.IDENTITY_UNCLEAR,
        priority="normal",
        reason="identity_policy_unavailable",
    )


request_callback_schema: dict[str, Any] = {
    "name": "request_callback",
    "description": "Create an anonymous clinic staff callback request for the trusted inbound call.",
    "parameters": {"type": "object", "properties": {}},
}


async def request_callback(args: dict[str, Any]) -> dict[str, Any]:
    return await _create_task_tool(args, InboundStaffTaskKind.CALLBACK, priority="normal")


escalate_inbound_to_staff_schema: dict[str, Any] = {
    "name": "escalate_inbound_to_staff",
    "description": "Create an anonymous-capable inbound staff escalation for clinical, urgent, complaint, safeguarding, distress, or unclear calls.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": sorted(INBOUND_ESCALATION_REASONS),
            },
        },
        "required": ["reason"],
    },
}


async def escalate_inbound_to_staff(args: dict[str, Any]) -> dict[str, Any]:
    requested_reason = str(args.get("reason") or "").strip().lower()
    reason = (
        requested_reason
        if requested_reason in INBOUND_ESCALATION_REASONS
        else "ambiguous"
    )
    priority = "high" if reason in TERMINAL_ESCALATION_REASONS else "normal"
    return await _create_task_tool(args, InboundStaffTaskKind.ESCALATION, priority=priority, reason=reason)


async def _create_task_tool(
    args: dict[str, Any], kind: InboundStaffTaskKind, *, priority: str, reason: str | None = None
) -> dict[str, Any]:
    try:
        _require_inbound(args)
        with get_sessionmaker()() as session:
            result = create_inbound_staff_task(
                session,
                _trusted(args, "clinic_id"),
                inbound_call_id=_trusted(args, "inbound_call_id"),
                kind=kind,
                priority=priority,
                reason=reason,
                summary=_generic_task_summary(kind, reason),
                payload={},
                now=_now(args),
            )
            session.commit()
        return {
            "success": True,
            "task_id": result.task_id,
            "kind": result.kind.value,
            "status": result.status.value,
            "priority": result.priority,
            "created": result.created,
            "idempotent": result.idempotent,
            "upgraded": result.upgraded,
        }
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


def _generic_task_summary(
    kind: InboundStaffTaskKind,
    reason: str | None,
) -> str:
    if kind == InboundStaffTaskKind.CALLBACK:
        return "Anonymous callback request requires staff follow-up."
    if kind == InboundStaffTaskKind.IDENTITY_UNCLEAR:
        return "Generic booking request requires staff review."
    labels = {
        "ambiguous": "ambiguous request",
        "clinical": "clinical concern",
        "complaint": "complaint",
        "distress": "distress concern",
        "identity_policy_unavailable": "identity verification request",
        "safeguarding": "safeguarding concern",
        "urgent": "urgent concern",
    }
    return f"Inbound {labels.get(reason or '', 'request')} requires staff review."


log_inbound_call_outcome_schema: dict[str, Any] = {
    "name": "log_inbound_call_outcome",
    "description": "Record a minimized non-clinical inbound call outcome.",
    "parameters": {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": list(INBOUND_OUTCOME_CODES),
            },
        },
        "required": ["outcome"],
    },
}


async def log_inbound_call_outcome(args: dict[str, Any]) -> dict[str, Any]:
    try:
        _require_inbound(args)
        outcome = str(args.get("outcome") or "").strip().lower()
        if outcome not in INBOUND_OUTCOME_CODES:
            raise ValueError("unsupported inbound outcome")
        with get_sessionmaker()() as session:
            result = deterministic_log_inbound_call_outcome(
                session,
                _trusted(args, "clinic_id"),
                inbound_call_id=_trusted(args, "inbound_call_id"),
                outcome=outcome,
                summary="",
                now=_now(args),
            )
            session.commit()
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


record_consent_decision_schema: dict[str, Any] = {
    "name": "record_consent_decision",
    "description": "Record an explicit administrative contact-consent decision.",
    "parameters": {
        "type": "object",
        "properties": {
            "consent_type": {"type": "string", "enum": ["contact"]},
            "granted": {"type": "boolean"},
        },
        "required": ["consent_type", "granted"],
    },
}


async def record_consent_decision(args: dict[str, Any]) -> dict[str, Any]:
    try:
        _require_inbound(args)
        with get_sessionmaker()() as session:
            result = deterministic_record_consent_decision(
                session,
                _trusted(args, "clinic_id"),
                inbound_call_id=_trusted(args, "inbound_call_id"),
                consent_type=str(args.get("consent_type") or ""),
                granted=bool(args.get("granted")),
                now=_now(args),
            )
            session.commit()
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        return _result_error(exc)


for schema, executor in (
    (get_clinic_hours_schema, get_clinic_hours),
    (get_clinic_services_schema, get_clinic_services),
    (find_possible_patient_match_schema, find_possible_patient_match),
    (record_inbound_opt_out_schema, record_inbound_opt_out),
    (get_available_slots_schema, get_available_slots),
    (create_inbound_booking_request_schema, create_inbound_booking_request),
    (request_callback_schema, request_callback),
    (escalate_inbound_to_staff_schema, escalate_inbound_to_staff),
    (log_inbound_call_outcome_schema, log_inbound_call_outcome),
    (record_consent_decision_schema, record_consent_decision),
):
    register_tool(schema["name"], schema, executor, tags=TOOL_TAGS)


__all__ = [
    "create_inbound_booking_request",
    "escalate_inbound_to_staff",
    "find_possible_patient_match",
    "get_available_slots",
    "get_clinic_hours",
    "get_clinic_services",
    "log_inbound_call_outcome",
    "record_consent_decision",
    "record_inbound_opt_out",
    "request_callback",
]