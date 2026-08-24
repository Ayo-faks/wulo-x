"""Unit tests for the inbound SMS text interpretation adapter."""

from __future__ import annotations

import json

from src.clinic_recall.inbound_text_agent import InboundTextIntent, parse_inbound_text_intent


def _json_payload(**overrides: object) -> str:
    payload: dict[str, object] = {
        "intent": "booking",
        "safety": "safe",
        "booking_kind": "new",
        "time_preference": "next_tuesday_morning",
        "selected_slot_ref": None,
        "callback_requested": False,
        "reply_tone": "warm_concise",
        "confidence": 0.88,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parse_inbound_text_intent_accepts_valid_booking_json() -> None:
    result = parse_inbound_text_intent(_json_payload())

    assert result == InboundTextIntent(
        intent="booking",
        safety="safe",
        booking_kind="new",
        time_preference="next_tuesday_morning",
        selected_slot_ref=None,
        callback_requested=False,
        reply_tone="warm_concise",
        confidence=0.88,
    )


def test_parse_inbound_text_intent_accepts_server_offered_slot_ref() -> None:
    result = parse_inbound_text_intent(
        _json_payload(selected_slot_ref="2", time_preference=None),
        offered_slots=(
            {"ref": "1", "slot_id": "slot-1", "start_at": "2026-07-02T09:00:00+00:00"},
            {"ref": "2", "slot_id": "slot-2", "start_at": "2026-07-02T10:00:00+00:00"},
        ),
    )

    assert result is not None
    assert result.selected_slot_ref == "2"


def test_parse_inbound_text_intent_fails_closed_for_malformed_json() -> None:
    assert parse_inbound_text_intent("not-json") is None


def test_parse_inbound_text_intent_fails_closed_for_unsafe_safety() -> None:
    assert parse_inbound_text_intent(_json_payload(safety="unsafe")) is None


def test_parse_inbound_text_intent_fails_closed_for_caller_supplied_ids() -> None:
    assert parse_inbound_text_intent(_json_payload(slot_id="availability-slot-123")) is None
    assert parse_inbound_text_intent(_json_payload(patient_id="patient-123")) is None


def test_parse_inbound_text_intent_fails_closed_for_unknown_slot_ref() -> None:
    assert (
        parse_inbound_text_intent(
            _json_payload(selected_slot_ref="3"),
            offered_slots=({"ref": "1", "slot_id": "slot-1", "start_at": "2026-07-02T09:00:00+00:00"},),
        )
        is None
    )


def test_parse_inbound_text_intent_fails_closed_for_low_confidence() -> None:
    assert parse_inbound_text_intent(_json_payload(confidence=0.2)) is None