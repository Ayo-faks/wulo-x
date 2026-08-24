from apps.artagent.backend.api.v1.endpoints.sms import (
    _as_event_list,
    _extract_subscription_validation_code,
    _summarize_sms_event,
)


def test_extracts_eventgrid_subscription_validation_code() -> None:
    events = _as_event_list(
        [
            {
                "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent",
                "data": {"validationCode": "abc-123"},
            }
        ]
    )

    assert _extract_subscription_validation_code(events) == "abc-123"


def test_summarizes_sms_without_message_content() -> None:
    summary = _summarize_sms_event(
        {
            "id": "event-1",
            "eventType": "Microsoft.Communication.SMSReceived",
            "eventTime": "2026-06-26T10:00:00Z",
            "data": {
                "from": "+447700900001",
                "to": "+447700900002",
                "message": "TEST reply",
            },
        }
    )

    assert summary == {
        "event_type": "Microsoft.Communication.SMSReceived",
        "event_id": "event-1",
        # Privacy: raw phone numbers are never logged; only presence markers.
        "from": "SET",
        "to": "SET",
        "message_length": 10,
        "received_at": "2026-06-26T10:00:00Z",
    }
    assert "message" not in summary