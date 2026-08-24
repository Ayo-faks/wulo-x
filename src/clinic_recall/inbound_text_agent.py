"""Structured text interpretation for safe inbound clinic SMS turns."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.ml_logging import logging

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parents[2] / ".agentops" / "prompts" / "inbound-assistant.prompt.md"
_DEFAULT_MODEL = "gpt-4o-mini"
_MIN_CONFIDENCE = 0.55

_SMS_ADDENDUM = """

SMS channel adapter instructions:
- This adapter is called only after deterministic urgent/clinical/complaint/safeguarding/distress/opt-out pre-screening has passed. For ordinary appointment logistics, set `safety` to `safe`.
- Use `safety` = `unsafe` or `unknown` only if the SMS text itself still contains clinical, urgent, safeguarding, distress, complaint, or ambiguous safety content that should fail closed.
- Interpret the inbound SMS only; do not perform actions or confirm outcomes.
- Return only strict JSON matching the supplied schema. Do not include prose.
- Keep all caller-facing wording out of the JSON except normalized intent fields.
- Do not echo PHI, phone numbers, names, dates of birth, patient IDs, clinic IDs, slot IDs, provider names, or raw appointment history.
- Never trust caller-provided clinic, patient, provider, clinician, or slot identifiers.
- Use `selected_slot_ref` only to refer to one of the server-offered slot references supplied in context, such as `1` or `2`; never invent or return a database slot id.
- For SMS, avoid audio/phone-call phrasing. Prefer short, warm, concise interpretations.
""".strip()


@dataclass(frozen=True)
class InboundTextIntent:
    """LLM-produced natural-language interpretation; deterministic code still acts."""

    intent: str
    safety: str
    booking_kind: str | None = None
    time_preference: str | None = None
    selected_slot_ref: str | None = None
    callback_requested: bool = False
    reply_tone: str = "warm_concise"
    confidence: float = 0.0


def interpret_inbound_text(
    *,
    body: str,
    context_summary: dict[str, Any],
    offered_slots: tuple[dict[str, str], ...] = (),
) -> InboundTextIntent | None:
    """Interpret a safe inbound SMS turn as strict structured intent.

    The adapter is lazy and config-gated so local webhooks/tests without Azure
    OpenAI configuration keep the deterministic fallback path.
    """
    if not _is_enabled():
        return None
    try:
        from src.aoai.client import create_azure_openai_client

        client = create_azure_openai_client()
        response = client.chat.completions.create(
            model=_deployment_name(),
            temperature=0,
            max_tokens=240,
            messages=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _user_payload(body, context_summary, offered_slots)},
            ],
            response_format=_response_format(),
        )
        content = response.choices[0].message.content if response.choices else None
        return parse_inbound_text_intent(content, offered_slots=offered_slots)
    except Exception as exc:  # noqa: BLE001 - model fallback must fail closed to deterministic handling
        logger.info("Inbound SMS text interpretation unavailable; using deterministic fallback: %s", exc)
        return None


def parse_inbound_text_intent(
    content: str | None,
    *,
    offered_slots: tuple[dict[str, str], ...] = (),
) -> InboundTextIntent | None:
    """Parse and validate strict JSON returned by the text interpreter."""
    if not content:
        return None
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    intent = _string_value(raw.get("intent"))
    safety = _string_value(raw.get("safety"))
    if intent not in {"booking", "callback", "clinic_info", "chitchat", "unclear"}:
        return None
    if safety != "safe":
        return None
    if _contains_untrusted_identifier(raw):
        return None

    selected_slot_ref = _optional_string(raw.get("selected_slot_ref"))
    if selected_slot_ref and not _offered_slot_ref_exists(selected_slot_ref, offered_slots):
        return None

    confidence = _float_value(raw.get("confidence"), default=0.0)
    if confidence < _MIN_CONFIDENCE:
        return None

    booking_kind = _optional_string(raw.get("booking_kind"))
    if booking_kind not in {None, "new", "change_existing"}:
        return None

    return InboundTextIntent(
        intent=intent,
        safety=safety,
        booking_kind=booking_kind,
        time_preference=_optional_string(raw.get("time_preference")),
        selected_slot_ref=selected_slot_ref,
        callback_requested=bool(raw.get("callback_requested") is True),
        reply_tone=_optional_string(raw.get("reply_tone")) or "warm_concise",
        confidence=confidence,
    )


def _is_enabled() -> bool:
    return (os.getenv("CLINIC_RECALL_INBOUND_TEXT_AGENT_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _deployment_name() -> str:
    return (
        os.getenv("CLINIC_RECALL_INBOUND_TEXT_AGENT_DEPLOYMENT")
        or os.getenv("AZURE_OPENAI_DEPLOYMENT")
        or _DEFAULT_MODEL
    )


def _system_prompt() -> str:
    try:
        base_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        base_prompt = "You are Wulo-X Inbound Clinic Assistant. Follow the safety and tool-boundary rules."
    return f"{base_prompt}\n\n{_SMS_ADDENDUM}"


def _user_payload(
    body: str,
    context_summary: dict[str, Any],
    offered_slots: tuple[dict[str, str], ...],
) -> str:
    return json.dumps(
        {
            "sms_body": body,
            "deterministic_safety_precheck": "passed",
            "context_summary": context_summary,
            "offered_slots": [
                {
                    "ref": slot.get("ref"),
                    "start_at": slot.get("start_at"),
                    "end_at": slot.get("end_at"),
                    "display_label": _slot_display_label(slot),
                }
                for slot in offered_slots
            ],
        },
        separators=(",", ":"),
    )


def _slot_display_label(slot: dict[str, str]) -> str | None:
    raw_start = slot.get("start_at")
    if not raw_start:
        return None
    try:
        start_at = datetime.fromisoformat(raw_start).astimezone(UTC)
    except ValueError:
        return raw_start
    return start_at.strftime("%a %d %b, %H:%M UTC")


def _response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "inbound_sms_intent",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "intent",
                    "safety",
                    "booking_kind",
                    "time_preference",
                    "selected_slot_ref",
                    "callback_requested",
                    "reply_tone",
                    "confidence",
                ],
                "properties": {
                    "intent": {"type": "string", "enum": ["booking", "callback", "clinic_info", "chitchat", "unclear"]},
                    "safety": {"type": "string", "enum": ["safe", "unsafe", "unknown"]},
                    "booking_kind": {"type": ["string", "null"], "enum": ["new", "change_existing", None]},
                    "time_preference": {"type": ["string", "null"], "maxLength": 80},
                    "selected_slot_ref": {"type": ["string", "null"], "maxLength": 16},
                    "callback_requested": {"type": "boolean"},
                    "reply_tone": {"type": "string", "enum": ["warm_concise"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
    }


def _contains_untrusted_identifier(raw: dict[str, Any]) -> bool:
    forbidden_keys = {
        "clinic_id",
        "patient_id",
        "provider_id",
        "provider_name",
        "clinician_name",
        "slot_id",
        "appointment_id",
    }
    return any(key in raw for key in forbidden_keys)


def _offered_slot_ref_exists(selected_slot_ref: str, offered_slots: tuple[dict[str, str], ...]) -> bool:
    return any(slot.get("ref") == selected_slot_ref for slot in offered_slots)


def _string_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _optional_string(value: Any) -> str | None:
    clean = str(value or "").strip().lower()
    return clean or None


def _float_value(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default