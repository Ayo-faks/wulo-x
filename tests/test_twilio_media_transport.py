import asyncio
import base64
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from apps.artagent.backend.voice.twilio import handler as handler_module
from apps.artagent.backend.voice.twilio.handler import TwilioVoiceLiveHandler
from apps.artagent.backend.voice.twilio.protocol import (
    TWILIO_MEDIA_CHANNELS,
    TWILIO_MEDIA_ENCODING,
    TWILIO_MEDIA_SAMPLE_RATE,
    TwilioProtocol,
)
from azure.ai.voicelive.models import ServerEventType
from fastapi.websockets import WebSocketState


def _twilio_handler_with_cancel(cancel: AsyncMock | None = None) -> tuple[TwilioVoiceLiveHandler, AsyncMock]:
    response_cancel = cancel or AsyncMock()
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._running = True
    handler._protocol.stream_sid = "MZ123"
    handler._connection = SimpleNamespace(response=SimpleNamespace(cancel=response_cancel))
    return handler, response_cancel


def _queued_items(handler: TwilioVoiceLiveHandler) -> list[dict | None]:
    items: list[dict | None] = []
    while not handler._outbound_queue.empty():
        items.append(handler._outbound_queue.get_nowait())
    return items


@pytest.mark.parametrize(
    ("record_call", "provider_confirmed", "expected"),
    [
        ("true", False, False),
        ("false", True, True),
    ],
)
async def test_recording_setup_uses_provider_confirmed_ledger_only(
    monkeypatch: pytest.MonkeyPatch,
    record_call: str,
    provider_confirmed: bool,
    expected: bool,
) -> None:
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._protocol.call_sid = "CA" + "a" * 32
    handler._protocol.custom_parameters = {
        "source": "clinic_recall_inbound",
        "clinic_id": "clinic-a",
        "record_call": record_call,
    }
    monkeypatch.setattr(
        handler,
        "_recording_authority_state",
        lambda: "confirmed" if provider_confirmed else "closed",
        raising=False,
    )

    handler._setup_consented_recording()
    if handler._recording_setup_task is not None:
        await handler._recording_setup_task

    assert handler._recording_enabled is expected


async def test_recording_authority_arms_after_delayed_provider_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._protocol.call_sid = "CA" + "a" * 32
    handler._protocol.custom_parameters = {
        "source": "clinic_recall_inbound",
        "clinic_id": "clinic-a",
        "record_call": "false",
    }
    states = iter(("pending", "confirmed"))
    monkeypatch.setattr(handler, "_recording_authority_state", lambda: next(states))
    handler._recording_authority_sleep = AsyncMock()

    handler._setup_consented_recording()
    assert handler._recording_enabled is False
    assert handler._recording_setup_task is not None
    await handler._recording_setup_task

    assert handler._recording_enabled is True
    handler._recording_authority_sleep.assert_awaited_once()


async def test_recording_authority_revokes_transcript_capture_after_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._running = True
    handler._recording_enabled = True
    handler._recording_authority_sleep = AsyncMock()
    monkeypatch.setattr(handler, "_recording_authority_state", lambda: "closed")

    await handler._monitor_recording_revocation()

    assert handler._recording_enabled is False
    handler._recording_authority_sleep.assert_awaited_once()


async def test_withdrawal_dtmf_is_not_forwarded_to_voicelive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._protocol.call_sid = "CA" + "a" * 32
    handler._protocol.custom_parameters = {
        "source": "clinic_recall_inbound",
        "clinic_id": "clinic-a",
    }
    handler._connection = SimpleNamespace(send=AsyncMock())
    handler._orchestrator = object()
    handler._recording_enabled = True
    withdraw = AsyncMock(return_value=True)
    monkeypatch.setattr(handler, "_withdraw_recording_consent", withdraw, raising=False)

    await handler._handle_dtmf("9")

    withdraw.assert_awaited_once()
    assert handler._recording_enabled is False
    handler._connection.send.assert_not_awaited()


async def test_unconfirmed_withdrawal_terminates_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._protocol.call_sid = "CA" + "a" * 32
    handler._protocol.custom_parameters = {
        "source": "clinic_recall_inbound",
        "clinic_id": "clinic-a",
    }
    handler._connection = SimpleNamespace(send=AsyncMock())
    handler._orchestrator = object()
    monkeypatch.setattr(handler, "_withdraw_recording_consent", AsyncMock(return_value=False))
    terminate = AsyncMock()
    monkeypatch.setattr(handler, "_terminate_twilio_call", terminate)

    await handler._handle_dtmf("9")

    terminate.assert_awaited_once_with("recording_withdrawal_unconfirmed")
    handler._connection.send.assert_not_awaited()


async def test_twilio_messenger_relay_logs_are_text_free(caplog) -> None:
    messenger = handler_module._TwilioMessenger("private-session-id")
    user_text = "private user message"
    assistant_text = "private assistant message"
    caplog.set_level(logging.INFO, logger="twilio.handler")

    await messenger.send_user_message(user_text)
    await messenger.send_assistant_message(assistant_text)

    assert user_text not in caplog.text
    assert assistant_text not in caplog.text
    assert "private-session-id" not in caplog.text
    assert "Twilio user message relayed | characters=20" in caplog.text
    assert "Twilio assistant message relayed | characters=25" in caplog.text


def test_twilio_protocol_extracts_start_context() -> None:
    protocol = TwilioProtocol("initial-session")

    protocol.process_start(
        {
            "event": "start",
            "start": {
                "streamSid": "MZ123",
                "callSid": "CA123",
                "accountSid": "AC123",
                "mediaFormat": {
                    "encoding": TWILIO_MEDIA_ENCODING,
                    "sampleRate": TWILIO_MEDIA_SAMPLE_RATE,
                    "channels": TWILIO_MEDIA_CHANNELS,
                },
                "customParameters": {
                    "session_id": "twilio-session-1",
                    "scenario": "rebooking",
                    "clinic_id": "clinic-a",
                    "patient_id": "patient-a",
                    "outreach_job_id": "job-a",
                },
            },
        }
    )

    assert protocol.session_id == "twilio-session-1"
    assert protocol.stream_sid == "MZ123"
    assert protocol.call_sid == "CA123"
    assert protocol.account_sid == "AC123"
    assert protocol.custom_parameters["scenario"] == "rebooking"


@pytest.mark.parametrize(
    ("field", "unsupported_value"),
    [
        ("encoding", "audio/pcm"),
        ("sampleRate", 24000),
        ("channels", 2),
    ],
)
def test_twilio_protocol_rejects_unsupported_media_format(
    field: str,
    unsupported_value: str | int,
) -> None:
    protocol = TwilioProtocol("initial-session")
    media_format: dict[str, str | int] = {
        "encoding": TWILIO_MEDIA_ENCODING,
        "sampleRate": TWILIO_MEDIA_SAMPLE_RATE,
        "channels": TWILIO_MEDIA_CHANNELS,
    }
    media_format[field] = unsupported_value

    with pytest.raises(ValueError, match="unsupported Twilio media format"):
        protocol.process_start(
            {
                "event": "start",
                "start": {
                    "streamSid": "MZ123",
                    "callSid": "CA123",
                    "accountSid": "AC123",
                    "mediaFormat": media_format,
                },
            }
        )

    assert protocol.stream_sid is None
    assert protocol.call_sid is None


async def test_twilio_handler_resolves_scenario_from_custom_parameters(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_resolve_orchestrator_config(*, session_id: str, scenario_name: str):
        captured["session_id"] = session_id
        captured["scenario_name"] = scenario_name
        return SimpleNamespace(has_scenario=False, agents={}, start_agent="InboundClinicAgent", handoff_map={})

    monkeypatch.setattr(handler_module, "discover_agents", lambda: {})
    monkeypatch.setattr(handler_module, "get_session_agent", lambda _session_id: None)
    monkeypatch.setattr(handler_module, "build_handoff_map", lambda _agents: {})
    monkeypatch.setattr(handler_module, "resolve_orchestrator_config", fake_resolve_orchestrator_config)
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._protocol.custom_parameters = {
        "scenario": "inbound_clinic",
        "provider": "twilio",
        "inbound_call_id": "inbound-call-a",
    }

    _agents, _config, start_agent, _handoff = await handler._resolve_agents()

    assert captured == {"session_id": "twilio-session-1", "scenario_name": "inbound_clinic"}
    assert start_agent == "InboundClinicAgent"


def test_twilio_protocol_round_trips_media_payload() -> None:
    protocol = TwilioProtocol("twilio-session-1")
    protocol.stream_sid = "MZ123"
    media = protocol.create_media(b"\xff\x7f")

    assert media == {
        "event": "media",
        "streamSid": "MZ123",
        "media": {"payload": base64.b64encode(b"\xff\x7f").decode("ascii")},
    }
    assert protocol.media_payload(media) == b"\xff\x7f"


async def test_twilio_handler_forwards_inbound_audio_to_voicelive() -> None:
    class InputBuffer:
        def __init__(self) -> None:
            self.append = AsyncMock()

    class Connection:
        def __init__(self) -> None:
            self.input_audio_buffer = InputBuffer()

    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._running = True
    handler._session_opened = True
    handler._connection = Connection()

    await handler.handle_text_message(
        json.dumps(
            {
                "event": "media",
                "streamSid": "MZ123",
                "media": {"payload": base64.b64encode(b"\xff" * 160).decode("ascii")},
            }
        )
    )

    handler._connection.input_audio_buffer.append.assert_awaited_once()
    kwargs = handler._connection.input_audio_buffer.append.await_args.kwargs
    assert isinstance(kwargs["audio"], str)
    assert base64.b64decode(kwargs["audio"])


async def test_twilio_handler_media_does_not_reset_conversational_idle_activity() -> None:
    class InputBuffer:
        def __init__(self) -> None:
            self.append = AsyncMock()

    class Connection:
        def __init__(self) -> None:
            self.input_audio_buffer = InputBuffer()

    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._running = True
    handler._session_opened = True
    handler._connection = Connection()
    handler._last_activity_ts = 123.0

    await handler.handle_text_message(
        json.dumps(
            {
                "event": "media",
                "streamSid": "MZ123",
                "media": {"payload": base64.b64encode(b"\xff" * 160).decode("ascii")},
            }
        )
    )

    assert handler._last_activity_ts == 123.0


async def test_twilio_handler_local_barge_in_clears_before_server_vad() -> None:
    class InputBuffer:
        def __init__(self) -> None:
            self.append = AsyncMock()

    cancel = AsyncMock()
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._LOCAL_BARGE_IN_ENABLED = True  # opt-in: default is off (echo-unsafe on PSTN)
    handler._running = True
    handler._session_opened = True
    handler._protocol.stream_sid = "MZ123"
    handler._connection = SimpleNamespace(input_audio_buffer=InputBuffer(), response=SimpleNamespace(cancel=cancel))
    handler._is_playing = True
    handler._active_response_ids.add("resp-1")

    media = {
        "event": "media",
        "streamSid": "MZ123",
        "media": {"payload": base64.b64encode(bytes([0x80]) * 160).decode("ascii")},
    }

    await handler.handle_text_message(json.dumps(media))
    await handler.handle_text_message(json.dumps(media))

    assert {"event": "clear", "streamSid": "MZ123"} in _queued_items(handler)
    cancel.assert_awaited_once()
    assert "resp-1" in handler._interrupted_response_ids
    assert handler._connection.input_audio_buffer.append.await_count == 2


async def test_twilio_handler_local_barge_in_disabled_by_default() -> None:
    """Echo of the agent's own audio must not cancel playout: the raw-energy
    local barge-in detector defaults OFF (2026-07-08 live echo misfires);
    server semantic VAD with echo cancellation owns barge-in."""

    class InputBuffer:
        def __init__(self) -> None:
            self.append = AsyncMock()

    cancel = AsyncMock()
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    assert handler._LOCAL_BARGE_IN_ENABLED is False
    handler._running = True
    handler._session_opened = True
    handler._protocol.stream_sid = "MZ123"
    handler._connection = SimpleNamespace(input_audio_buffer=InputBuffer(), response=SimpleNamespace(cancel=cancel))
    handler._is_playing = True
    handler._active_response_ids.add("resp-1")

    media = {
        "event": "media",
        "streamSid": "MZ123",
        "media": {"payload": base64.b64encode(bytes([0x80]) * 160).decode("ascii")},
    }

    await handler.handle_text_message(json.dumps(media))
    await handler.handle_text_message(json.dumps(media))

    assert all(item.get("event") != "clear" for item in _queued_items(handler) if item is not None)
    cancel.assert_not_awaited()
    assert handler._connection.input_audio_buffer.append.await_count == 2


async def test_twilio_handler_local_barge_in_ignores_silence() -> None:
    class InputBuffer:
        def __init__(self) -> None:
            self.append = AsyncMock()

    cancel = AsyncMock()
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._LOCAL_BARGE_IN_ENABLED = True  # opt-in: default is off (echo-unsafe on PSTN)
    handler._running = True
    handler._session_opened = True
    handler._protocol.stream_sid = "MZ123"
    handler._connection = SimpleNamespace(input_audio_buffer=InputBuffer(), response=SimpleNamespace(cancel=cancel))
    handler._is_playing = True
    handler._active_response_ids.add("resp-1")

    media = {
        "event": "media",
        "streamSid": "MZ123",
        "media": {"payload": base64.b64encode(bytes([0xFF]) * 160).decode("ascii")},
    }

    await handler.handle_text_message(json.dumps(media))
    await handler.handle_text_message(json.dumps(media))

    assert all(item.get("event") != "clear" for item in _queued_items(handler) if item is not None)
    cancel.assert_not_awaited()
    assert handler._connection.input_audio_buffer.append.await_count == 2


async def test_twilio_handler_local_barge_in_ignores_audio_when_assistant_not_playing() -> None:
    class InputBuffer:
        def __init__(self) -> None:
            self.append = AsyncMock()

    cancel = AsyncMock()
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._LOCAL_BARGE_IN_ENABLED = True  # opt-in: default is off (echo-unsafe on PSTN)
    handler._running = True
    handler._session_opened = True
    handler._protocol.stream_sid = "MZ123"
    handler._connection = SimpleNamespace(input_audio_buffer=InputBuffer(), response=SimpleNamespace(cancel=cancel))

    media = {
        "event": "media",
        "streamSid": "MZ123",
        "media": {"payload": base64.b64encode(bytes([0x80]) * 160).decode("ascii")},
    }

    await handler.handle_text_message(json.dumps(media))
    await handler.handle_text_message(json.dumps(media))

    assert all(item.get("event") != "clear" for item in _queued_items(handler) if item is not None)
    cancel.assert_not_awaited()
    assert handler._connection.input_audio_buffer.append.await_count == 2


async def test_twilio_handler_conversational_events_reset_idle_activity(monkeypatch) -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    handler._last_activity_ts = 0.0

    await handler._handle_voicelive_event(
        SimpleNamespace(item_id="item-echo"),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    speech_activity = handler._last_activity_ts

    handler._last_activity_ts = 0.0
    handler._barge_in_audio_suppression_until = 0.0
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"assistant-audio")
    await handler._handle_voicelive_event(
        SimpleNamespace(delta="assistant-audio", response_id="resp-1"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )
    audio_activity = handler._last_activity_ts

    handler._last_activity_ts = 0.0
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-1"),
        ServerEventType.RESPONSE_DONE,
    )

    assert speech_activity > 0
    assert audio_activity > 0
    assert handler._last_activity_ts > 0
    if handler._pacer_task and not handler._pacer_task.done():
        handler._pacer_task.cancel()


async def test_twilio_handler_speech_start_queues_clear_and_drops_queued_media() -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    handler._outbound_queue.put_nowait(handler._protocol.create_media(b"stale-audio"))

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )

    items = _queued_items(handler)
    assert {"event": "clear", "streamSid": "MZ123"} in items
    assert all(item is None or item.get("event") != "media" for item in items)


async def test_twilio_handler_speech_start_clears_buffered_outbound_audio() -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    handler._audio_accum.extend(b"buffered-audio")

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )

    assert handler._audio_accum == bytearray()


async def test_twilio_handler_speech_start_attempts_voicelive_cancel() -> None:
    handler, cancel = _twilio_handler_with_cancel()

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )

    cancel.assert_awaited_once()


async def test_twilio_handler_speech_start_ignores_benign_cancel_failure() -> None:
    cancel = AsyncMock(side_effect=RuntimeError("response_cancel_not_active"))
    handler, _cancel = _twilio_handler_with_cancel(cancel)

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )

    assert {"event": "clear", "streamSid": "MZ123"} in _queued_items(handler)
    cancel.assert_awaited_once()


async def test_twilio_handler_drops_late_audio_deltas_after_barge_in(monkeypatch) -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    handler._active_response_ids.add("resp-1")
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"stale-audio")

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(delta="late-delta", response_id="resp-1"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-1"),
        ServerEventType.RESPONSE_DONE,
    )

    items = _queued_items(handler)
    assert {"event": "clear", "streamSid": "MZ123"} in items
    assert all(item is None or item.get("event") != "media" for item in items)
    assert handler._audio_accum == bytearray()


async def test_twilio_handler_drops_new_response_audio_during_barge_in_suppression(monkeypatch) -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"next-audio")

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(delta="new-delta", response_id="resp-next"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )

    assert handler._audio_accum == bytearray()


async def test_twilio_handler_releases_barge_in_suppression_after_transcript(monkeypatch) -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"next-audio")

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(transcript="Yes please."),
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(delta="new-delta", response_id="resp-next"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(delta="Okay,", response_id="resp-next"),
        ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
    )

    assert handler._audio_accum == bytearray(b"next-audio")
    if handler._pacer_task and not handler._pacer_task.done():
        handler._pacer_task.cancel()


async def test_twilio_handler_still_drops_interrupted_response_after_transcript(monkeypatch) -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    handler._active_response_ids.add("resp-old")
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"old-audio")

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(transcript="Yes please."),
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(delta="old-delta", response_id="resp-old"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )

    assert handler._audio_accum == bytearray()


async def test_twilio_handler_allows_response_audio_after_barge_in_suppression_expires(monkeypatch) -> None:
    now = 100.0
    handler, _cancel = _twilio_handler_with_cancel()
    monkeypatch.setattr(handler_module.time, "perf_counter", lambda: now)
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"next-audio")

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    now = 105.0
    await handler._handle_voicelive_event(
        SimpleNamespace(delta="new-delta", response_id="resp-next"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(delta="Okay,", response_id="resp-next"),
        ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
    )

    assert handler._audio_accum == bytearray(b"next-audio")
    if handler._pacer_task and not handler._pacer_task.done():
        handler._pacer_task.cancel()


async def test_twilio_handler_tracks_response_created_before_audio_for_barge_in() -> None:
    handler, _cancel = _twilio_handler_with_cancel()

    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-created")),
        ServerEventType.RESPONSE_CREATED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )

    assert "resp-created" in handler._interrupted_response_ids


async def test_twilio_handler_quarantines_audio_until_transcript_prefix_is_safe(monkeypatch) -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    enqueue_audio = AsyncMock()
    monkeypatch.setattr(handler, "_enqueue_audio", enqueue_audio)
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"voice")

    await handler._handle_voicelive_event(
        SimpleNamespace(delta="audio", response_id="resp-safe"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )
    enqueue_audio.assert_not_awaited()

    await handler._handle_voicelive_event(
        SimpleNamespace(delta="assistant", response_id="resp-safe"),
        ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
    )
    enqueue_audio.assert_not_awaited()
    await handler._handle_voicelive_event(
        SimpleNamespace(delta=" can help with that.", response_id="resp-safe"),
        ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
    )

    enqueue_audio.assert_awaited_once_with(b"voice")
    assert "resp-safe" in handler._safe_assistant_response_ids


async def test_twilio_handler_audio_done_preserves_undecided_quarantine(monkeypatch) -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    enqueue_audio = AsyncMock()
    monkeypatch.setattr(handler, "_enqueue_audio", enqueue_audio)
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"voice")

    await handler._handle_voicelive_event(
        SimpleNamespace(delta="audio", response_id="resp-late-transcript"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-late-transcript"),
        ServerEventType.RESPONSE_AUDIO_DONE,
    )

    assert handler._quarantined_response_audio["resp-late-transcript"] == bytearray(b"voice")
    enqueue_audio.assert_not_awaited()

    await handler._handle_voicelive_event(
        SimpleNamespace(transcript="Here are the hours.", response_id="resp-late-transcript"),
        ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE,
    )
    enqueue_audio.assert_awaited_once_with(b"voice")


async def test_twilio_handler_response_done_uses_final_transcript_to_release_audio(monkeypatch) -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    enqueue_audio = AsyncMock()
    monkeypatch.setattr(handler, "_enqueue_audio", enqueue_audio)
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"voice")

    await handler._handle_voicelive_event(
        SimpleNamespace(delta="audio", response_id="resp-final-transcript"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(
            response=SimpleNamespace(
                id="resp-final-transcript",
                output=[
                    SimpleNamespace(
                        content=[SimpleNamespace(transcript="The clinic opens at nine.")]
                    )
                ],
            )
        ),
        ServerEventType.RESPONSE_DONE,
    )

    enqueue_audio.assert_awaited_once_with(b"voice")


async def test_twilio_handler_blocks_internal_function_syntax_before_playout(monkeypatch) -> None:
    handler, cancel = _twilio_handler_with_cancel()
    enqueue_audio = AsyncMock()
    monkeypatch.setattr(handler, "_enqueue_audio", enqueue_audio)
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"internal")

    await handler._handle_voicelive_event(
        SimpleNamespace(delta="audio", response_id="resp-internal"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(
            delta="assistant to=functions.get_clinic_hours",
            response_id="resp-internal",
        ),
        ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
    )

    enqueue_audio.assert_not_awaited()
    cancel.assert_awaited_once()
    assert "resp-internal" in handler._blocked_assistant_response_ids
    assert "resp-internal" in handler._interrupted_response_ids
    assert handler._quarantined_response_audio == {}
    assert {"event": "clear", "streamSid": "MZ123"} in _queued_items(handler)

    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-internal"),
        ServerEventType.RESPONSE_AUDIO_DONE,
    )
    assert "resp-internal" in handler._blocked_assistant_response_ids

    handler._pending_call_end = True
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-internal"),
        ServerEventType.RESPONSE_DONE,
    )
    assert "resp-internal" not in handler._blocked_assistant_response_ids
    assert handler._call_end_response_done is True


async def test_twilio_handler_drops_unknown_late_audio_during_barge_in_fallback(monkeypatch) -> None:
    now = 100.0
    handler, _cancel = _twilio_handler_with_cancel()
    monkeypatch.setattr(handler_module.time, "perf_counter", lambda: now)
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"unknown-stale-audio")

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(delta="late-delta"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )

    assert handler._audio_accum == bytearray()


async def test_twilio_handler_response_done_for_interrupted_response_does_not_flush_stale_media() -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    handler._active_response_ids.add("resp-1")

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    handler._audio_accum.extend(b"stale-flush")
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-1"),
        ServerEventType.RESPONSE_DONE,
    )

    items = _queued_items(handler)
    assert {"event": "clear", "streamSid": "MZ123"} in items
    assert all(item is None or item.get("event") != "media" for item in items)
    assert handler._audio_accum == bytearray()


async def test_twilio_handler_response_done_keeps_normal_drain_nonblocking() -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    handler._audio_accum.extend(b"x" * (handler._AUDIO_CHUNK_SIZE * 2))

    await asyncio.wait_for(
        handler._handle_voicelive_event(
            SimpleNamespace(response_id="resp-new"),
            ServerEventType.RESPONSE_DONE,
        ),
        timeout=0.05,
    )

    assert handler._pacer_task is not None
    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )

    items = _queued_items(handler)
    assert {"event": "clear", "streamSid": "MZ123"} in items
    assert handler._audio_accum == bytearray()
    assert handler._pacer_task is None


async def test_twilio_writer_sends_media_after_barge_in() -> None:
    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.send_text = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = False
    handler._protocol.stream_sid = "MZ123"

    await handler._outbound_queue.put(handler._protocol.create_media(b"next-audio"))

    await handler._outbound_writer()

    websocket.send_text.assert_awaited_once()


async def test_twilio_writer_drops_queued_media_during_known_barge_in(monkeypatch) -> None:
    now = 100.0

    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.send_text = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = False
    handler._protocol.stream_sid = "MZ123"
    handler._interrupted_response_ids.add("resp-1")
    handler._barge_in_audio_suppression_until = 101.0
    monkeypatch.setattr(handler_module.time, "perf_counter", lambda: now)

    await handler._outbound_queue.put(handler._protocol.create_media(b"stale-audio"))

    await handler._outbound_writer()

    websocket.send_text.assert_not_awaited()


async def test_twilio_writer_drops_previous_playout_generation_media() -> None:
    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.send_text = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = False
    handler._protocol.stream_sid = "MZ123"

    await handler._outbound_queue.put(handler._create_media_message(b"old-audio"))
    handler._playout_generation += 1

    await handler._outbound_writer()

    websocket.send_text.assert_not_awaited()


async def test_twilio_writer_strips_internal_playout_generation() -> None:
    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.send_text = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = False
    handler._protocol.stream_sid = "MZ123"

    await handler._outbound_queue.put(handler._create_media_message(b"next-audio"))

    await handler._outbound_writer()

    sent = json.loads(websocket.send_text.await_args.args[0])
    assert sent == handler._protocol.create_media(b"next-audio")
    assert "_wulo_playout_generation" not in sent


async def test_twilio_writer_sends_media_after_unknown_fallback_expires(monkeypatch) -> None:
    now = 100.0

    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.send_text = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = False
    handler._protocol.stream_sid = "MZ123"
    handler._unknown_response_fallback_until = 101.0
    monkeypatch.setattr(handler_module.time, "perf_counter", lambda: now)

    await handler._outbound_queue.put(handler._protocol.create_media(b"stale-audio"))

    await handler._outbound_writer()

    websocket.send_text.assert_not_awaited()

    now = 102.0
    await handler._outbound_queue.put(handler._protocol.create_media(b"next-audio"))

    await handler._outbound_writer()

    websocket.send_text.assert_awaited_once()


async def test_twilio_messenger_request_call_end_marks_pending(monkeypatch) -> None:
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")

    await handler._messenger.request_call_end(reason="clinical")

    assert handler._pending_call_end is True
    assert handler._call_end_reason == "clinical"
    assert handler._call_end_task is not None
    await handler._call_end_task  # _running is False, so the terminator returns promptly


async def test_twilio_terminal_call_end_suppresses_active_stale_response(monkeypatch) -> None:
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._active_response_ids.add("resp-stale")
    handler._audio_accum.extend(b"stale")

    handler._mark_pending_call_end("clinical")

    assert "resp-stale" in handler._interrupted_response_ids
    assert not handler._active_response_ids
    assert not handler._audio_accum
    assert handler._should_drop_interrupted_audio("resp-stale", now=time.perf_counter()) is True
    if handler._call_end_task:
        await handler._call_end_task


async def test_twilio_barge_in_cancels_interruptible_pending_call_end(monkeypatch) -> None:
    handler, response_cancel = _twilio_handler_with_cancel()
    termination_started = asyncio.Event()

    async def wait_for_termination(_reason: str, *, generation: int | None = None) -> None:
        termination_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(handler, "_terminate_twilio_call", wait_for_termination)
    handler._mark_pending_call_end("clinical")
    await termination_started.wait()

    await handler._handle_barge_in()

    assert handler._pending_call_end is False
    assert handler._call_end_reason == ""
    assert handler._call_end_task is None
    response_cancel.assert_awaited_once()


async def test_twilio_barge_in_does_not_cancel_urgent_hard_stop(monkeypatch) -> None:
    handler, response_cancel = _twilio_handler_with_cancel()
    termination_started = asyncio.Event()

    async def wait_for_termination(_reason: str, *, generation: int | None = None) -> None:
        termination_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(handler, "_terminate_twilio_call", wait_for_termination)
    handler._mark_pending_call_end("urgent")
    await termination_started.wait()

    await handler._handle_barge_in()

    assert handler._pending_call_end is True
    assert handler._call_end_reason == "urgent"
    assert handler._call_end_task is not None
    assert not handler._call_end_task.done()
    response_cancel.assert_not_awaited()
    assert {"event": "clear", "streamSid": "MZ123"} not in _queued_items(handler)

    handler._call_end_task.cancel()
    try:
        await handler._call_end_task
    except asyncio.CancelledError:
        pass


async def test_twilio_urgent_hard_stop_protects_governed_candidate() -> None:
    handler, _response_cancel = _twilio_handler_with_cancel()
    handler._governed_response_pending = True
    handler._governed_response_candidates.add("resp-urgent")
    handler._active_response_ids.update({"resp-old", "resp-urgent"})
    handler._interrupted_response_ids.add("resp-urgent")

    handler._mark_pending_call_end("urgent")

    assert "resp-urgent" not in handler._interrupted_response_ids
    assert "resp-old" in handler._interrupted_response_ids
    assert handler._active_response_ids == {"resp-urgent"}
    if handler._call_end_task:
        handler._call_end_task.cancel()
        try:
            await handler._call_end_task
        except asyncio.CancelledError:
            pass


async def test_twilio_interrupted_terminal_response_done_releases_call_end() -> None:
    handler, _response_cancel = _twilio_handler_with_cancel()
    handler._pending_call_end = True
    handler._interrupted_response_ids.add("resp-terminal")

    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-terminal"),
        ServerEventType.RESPONSE_DONE,
    )

    assert handler._call_end_response_done is True


async def _hang_forever(_reason: str, *, generation: int | None = None) -> None:
    await asyncio.Event().wait()


async def _cancel_call_end_task(handler: TwilioVoiceLiveHandler) -> None:
    task = handler._call_end_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_twilio_pre_arm_barge_in_state_cleared_by_hard_stop_arm(monkeypatch) -> None:
    """Pre-arm caller speech must not mute the urgent governed close-out.

    Live probes CAea0731/CA6dfeef (2026-07-10): a barge-in during the
    escalation write set a 4s suppression window that survived hard-stop
    arming, so the protected governed response's audio could be dropped and
    ``_call_end_audio_seen`` never set.
    """
    handler, _response_cancel = _twilio_handler_with_cancel()
    monkeypatch.setattr(handler, "_terminate_twilio_call", _hang_forever)

    await handler._handle_barge_in()  # pre-arm: no call end pending yet
    assert handler._barge_in_audio_suppression_until > 0.0

    handler._mark_pending_call_end("urgent")

    assert handler._barge_in_audio_suppression_until == 0.0
    assert handler._unknown_response_fallback_until == 0.0
    assert handler._governed_interrupted_before_claim is False

    # The governed create follows arming; its response must stay audible.
    handler._governed_response_pending = True
    handler._governed_expected_text = "Exact urgent signpost."
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-urgent"),
        ServerEventType.RESPONSE_CREATED,
    )
    assert "resp-urgent" not in handler._interrupted_response_ids
    assert handler._should_drop_interrupted_audio("resp-urgent", now=time.perf_counter()) is False

    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"urgent-audio")
    await handler._handle_voicelive_event(
        SimpleNamespace(delta="urgent-delta", response_id="resp-urgent"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )
    assert handler._call_end_audio_seen is True

    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-urgent"),
        ServerEventType.RESPONSE_DONE,
    )
    assert handler._call_end_response_done is True

    if handler._pacer_task and not handler._pacer_task.done():
        handler._pacer_task.cancel()
    await _cancel_call_end_task(handler)


async def test_twilio_hard_stop_arm_clears_pre_claim_interruption_without_actives(monkeypatch) -> None:
    """Hard-stop protection must not be skipped when barge-in emptied actives."""
    handler, _response_cancel = _twilio_handler_with_cancel()
    monkeypatch.setattr(handler, "_terminate_twilio_call", _hang_forever)
    handler._governed_response_pending = True
    handler._governed_expected_text = "Exact urgent signpost."
    handler._governed_interrupted_before_claim = True  # barge-in landed pre-claim
    assert not handler._active_response_ids

    handler._mark_pending_call_end("urgent")

    assert handler._governed_interrupted_before_claim is False
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-urgent"),
        ServerEventType.RESPONSE_CREATED,
    )
    assert "resp-urgent" not in handler._interrupted_response_ids
    await _cancel_call_end_task(handler)


async def test_twilio_hard_stop_stale_audio_still_dropped_after_window_reset(monkeypatch) -> None:
    """Clearing the suppression window must not leak known-stale audio."""
    handler, _response_cancel = _twilio_handler_with_cancel()
    monkeypatch.setattr(handler, "_terminate_twilio_call", _hang_forever)
    handler._active_response_ids.add("resp-old")

    handler._mark_pending_call_end("urgent")

    assert handler._barge_in_audio_suppression_until == 0.0
    assert "resp-old" in handler._interrupted_response_ids
    assert handler._should_drop_interrupted_audio("resp-old", now=time.perf_counter()) is True
    await _cancel_call_end_task(handler)


async def test_twilio_non_governed_response_suppressed_during_hard_stop_close(monkeypatch) -> None:
    """A model/auto response created during a hard-stop close must never play.

    Live probe CAea0731 (2026-07-10): post-arm caller speech produced a
    model-authored reopen ("…Is there anything else you…") that played during
    the urgent close.
    """
    handler, _response_cancel = _twilio_handler_with_cancel()
    monkeypatch.setattr(handler, "_terminate_twilio_call", _hang_forever)
    handler._mark_pending_call_end("urgent")

    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-reopen"),
        ServerEventType.RESPONSE_CREATED,
    )

    assert "resp-reopen" in handler._interrupted_response_ids
    assert handler._should_drop_interrupted_audio("resp-reopen", now=time.perf_counter()) is True
    await _cancel_call_end_task(handler)


async def test_twilio_caller_turn_events_not_forwarded_during_hard_stop(monkeypatch) -> None:
    """Caller turns must not reach the orchestrator during a hard-stop close.

    Live probe CAea0731 (2026-07-10): the ignored barge-in's transcription
    still drove a second escalation write and a model follow-up response.
    """
    handler, _response_cancel = _twilio_handler_with_cancel()
    monkeypatch.setattr(handler, "_terminate_twilio_call", _hang_forever)
    handler._mark_pending_call_end("urgent")

    assert (
        handler._should_forward_event_to_orchestrator(
            ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA
        )
        is False
    )
    assert (
        handler._should_forward_event_to_orchestrator(
            ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED
        )
        is False
    )
    assert (
        handler._should_forward_event_to_orchestrator(
            ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED
        )
        is False
    )
    assert (
        handler._should_forward_event_to_orchestrator(
            ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED
        )
        is False
    )
    # Response lifecycle events keep flowing so the close-out completes.
    assert handler._should_forward_event_to_orchestrator(ServerEventType.RESPONSE_DONE) is True
    await _cancel_call_end_task(handler)

    # Interruptible closes keep forwarding so caller speech can cancel them.
    interruptible, _cancel2 = _twilio_handler_with_cancel()
    monkeypatch.setattr(interruptible, "_terminate_twilio_call", _hang_forever)
    interruptible._mark_pending_call_end("user_goodbye")
    assert (
        interruptible._should_forward_event_to_orchestrator(
            ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED
        )
        is True
    )
    await _cancel_call_end_task(interruptible)


async def test_twilio_hard_stop_upgrades_pending_interruptible_close(monkeypatch) -> None:
    """An urgent request during a pending interruptible close must upgrade it."""
    handler, response_cancel = _twilio_handler_with_cancel()
    started: list[str] = []

    async def record_termination(reason: str, *, generation: int | None = None) -> None:
        started.append(reason)
        await asyncio.Event().wait()

    monkeypatch.setattr(handler, "_terminate_twilio_call", record_termination)
    handler._mark_pending_call_end("user_goodbye")
    await asyncio.sleep(0)
    first_generation = handler._call_end_generation

    handler._mark_pending_call_end("urgent")

    assert handler._pending_call_end is True
    assert handler._call_end_reason == "urgent"
    assert handler._call_end_generation > first_generation
    await asyncio.sleep(0)
    assert started[-1] == "urgent"

    # Caller speech can no longer cancel the upgraded close.
    await handler._handle_barge_in()
    assert handler._pending_call_end is True
    assert handler._call_end_reason == "urgent"
    response_cancel.assert_not_awaited()
    await _cancel_call_end_task(handler)


async def test_twilio_hard_stop_reason_never_downgraded(monkeypatch) -> None:
    handler, _response_cancel = _twilio_handler_with_cancel()
    monkeypatch.setattr(handler, "_terminate_twilio_call", _hang_forever)
    handler._mark_pending_call_end("urgent")

    handler._mark_pending_call_end("user_goodbye")

    assert handler._call_end_reason == "urgent"
    await _cancel_call_end_task(handler)


async def test_twilio_stale_call_end_generation_cannot_reach_rest(monkeypatch) -> None:
    handler, _response_cancel = _twilio_handler_with_cancel()
    handler._pending_call_end = True
    handler._call_end_reason = "clinical"
    handler._call_end_generation = 2
    handler._complete_twilio_call_via_rest = AsyncMock(return_value=True)

    await handler._terminate_twilio_call("clinical", generation=1)

    handler._complete_twilio_call_via_rest.assert_not_awaited()


async def test_twilio_handler_audio_delta_marks_call_end_audio_seen(monkeypatch) -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    handler._pending_call_end = True
    monkeypatch.setattr(handler_module, "convert_voicelive_delta_to_ulaw", lambda _delta: b"sign-off")

    await handler._handle_voicelive_event(
        SimpleNamespace(delta="goodbye-audio", response_id="resp-final"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )

    assert handler._call_end_audio_seen is True
    if handler._pacer_task and not handler._pacer_task.done():
        handler._pacer_task.cancel()


async def test_twilio_handler_terminate_closes_ws_without_rest_creds(monkeypatch) -> None:
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)

    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.send_text = AsyncMock()
            self.close = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = True
    handler._protocol.stream_sid = "MZ123"
    handler._call_end_audio_seen = True  # skip the audio-start wait

    await handler._terminate_twilio_call("user_goodbye")

    websocket.close.assert_awaited_once()
    assert handler._shutdown.is_set()


async def test_twilio_handler_terminate_completes_call_via_rest(monkeypatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    captured: dict = {}

    class _FakeResponse:
        status_code = 204
        text = ""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, data=None, auth=None):
            captured["url"] = url
            captured["data"] = data
            captured["auth"] = auth
            return _FakeResponse()

    monkeypatch.setattr(handler_module.httpx, "AsyncClient", _FakeAsyncClient)

    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.send_text = AsyncMock()
            self.close = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = True
    handler._protocol.call_sid = "CA123"
    handler._call_end_audio_seen = True

    await handler._terminate_twilio_call("clinical")

    assert captured["data"] == {"Status": "completed"}
    assert "CA123" in captured["url"]
    assert captured["auth"] == ("AC123", "secret")
    websocket.close.assert_awaited_once()


async def test_twilio_handler_terminate_waits_for_call_end_mark(monkeypatch) -> None:
    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.close = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = True
    handler._protocol.stream_sid = "MZ123"
    handler._call_end_audio_seen = True
    handler._call_end_response_done = True
    handler._complete_twilio_call_via_rest = AsyncMock(return_value=True)

    task = asyncio.create_task(handler._terminate_twilio_call("booking_complete"))
    for _ in range(20):
        if not handler._outbound_queue.empty():
            break
        await asyncio.sleep(0)
    mark = handler._outbound_queue.get_nowait()

    assert mark["event"] == "mark"
    assert mark["streamSid"] == "MZ123"
    handler._complete_twilio_call_via_rest.assert_not_awaited()

    await handler.handle_text_message(json.dumps({"event": "mark", "mark": mark["mark"]}))
    await task

    handler._complete_twilio_call_via_rest.assert_awaited_once_with("booking_complete")
    websocket.close.assert_awaited_once()


async def test_twilio_handler_terminate_mark_timeout_still_hangs_up(monkeypatch) -> None:
    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.close = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = True
    handler._protocol.stream_sid = "MZ123"
    handler._call_end_audio_seen = True
    handler._CALL_END_MARK_TIMEOUT = 0.01
    handler._complete_twilio_call_via_rest = AsyncMock(return_value=True)

    await handler._terminate_twilio_call("booking_complete")

    queued = _queued_items(handler)
    assert any(item and item.get("event") == "mark" for item in queued)
    handler._complete_twilio_call_via_rest.assert_awaited_once_with("booking_complete")
    websocket.close.assert_awaited_once()


def test_twilio_handler_call_end_defaults_cover_governed_playout() -> None:
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")

    assert handler._CALL_END_MARK_TIMEOUT >= 20.0
    assert handler._CALL_END_MAX_WAIT >= 35.0


async def test_twilio_handler_call_end_mark_still_sends_after_barge_in_state(monkeypatch) -> None:
    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.close = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = True
    handler._protocol.stream_sid = "MZ123"
    handler._call_end_audio_seen = True
    handler._call_end_response_done = True
    handler._interrupted_response_ids.add("stale-response")
    handler._unknown_response_fallback_until = time.perf_counter() + 10
    handler._complete_twilio_call_via_rest = AsyncMock(return_value=True)

    task = asyncio.create_task(handler._terminate_twilio_call("user_goodbye"))
    for _ in range(20):
        if not handler._outbound_queue.empty():
            break
        await asyncio.sleep(0)
    mark = handler._outbound_queue.get_nowait()

    assert mark["event"] == "mark"
    await handler.handle_text_message(json.dumps({"event": "mark", "mark": mark["mark"]}))
    await task

    handler._complete_twilio_call_via_rest.assert_awaited_once_with("user_goodbye")
    websocket.close.assert_awaited_once()


async def test_twilio_handler_call_end_mark_reserves_time_after_queue_drain_timeout() -> None:
    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.close = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = True
    handler._protocol.stream_sid = "MZ123"
    handler._call_end_audio_seen = True
    handler._CALL_END_RESPONSE_DONE_TIMEOUT = 0.01
    handler._CALL_END_QUEUE_DRAIN_TIMEOUT = 0.01
    handler._CALL_END_MARK_TIMEOUT = 0.01
    handler._complete_twilio_call_via_rest = AsyncMock(return_value=True)
    await handler._outbound_queue.put(handler._protocol.create_media(b"already-sent-closeout"))

    await handler._terminate_twilio_call("assistant_goodbye")

    queued = _queued_items(handler)
    assert any(item and item.get("event") == "media" for item in queued)
    assert any(item and item.get("event") == "mark" for item in queued)
    handler._complete_twilio_call_via_rest.assert_awaited_once_with("assistant_goodbye")
    websocket.close.assert_awaited_once()


async def test_twilio_handler_call_end_buffered_audio_cannot_consume_mark_reserve() -> None:
    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.close = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = True
    handler._protocol.stream_sid = "MZ123"
    handler._call_end_audio_seen = True
    handler._call_end_response_done = True
    handler._CALL_END_MAX_WAIT = 0.15
    handler._CALL_END_QUEUE_DRAIN_TIMEOUT = 0.05
    handler._CALL_END_MARK_TIMEOUT = 0.05
    handler._audio_accum.extend(b"close-out" * handler._AUDIO_CHUNK_SIZE)
    handler._complete_twilio_call_via_rest = AsyncMock(return_value=True)

    await handler._terminate_twilio_call("urgent")

    queued = _queued_items(handler)
    assert any(item and item.get("event") == "mark" for item in queued)
    handler._complete_twilio_call_via_rest.assert_awaited_once_with("urgent")
    websocket.close.assert_awaited_once()


async def test_twilio_handler_response_done_without_pending_keeps_call_open() -> None:
    class WebSocket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self) -> None:
            self.send_text = AsyncMock()
            self.close = AsyncMock()

    websocket = WebSocket()
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = True
    handler._protocol.stream_sid = "MZ123"

    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-final"),
        ServerEventType.RESPONSE_DONE,
    )

    assert handler._call_end_task is None
    websocket.close.assert_not_awaited()


async def test_twilio_idle_monitor_terminates_after_conversational_timeout(monkeypatch) -> None:
    monkeypatch.setattr(handler_module, "_CONVERSATION_IDLE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(handler_module, "_CONVERSATION_IDLE_CHECK_INTERVAL_S", 0.01)

    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._running = True
    handler._last_activity_ts = handler_module.time.monotonic() - 10.0
    handler._terminate_twilio_call = AsyncMock()

    handler._start_idle_monitor()
    handler._last_activity_ts = handler_module.time.monotonic() - 10.0
    await asyncio.wait_for(handler._idle_task, timeout=0.2)

    handler._terminate_twilio_call.assert_awaited_once_with("idle_timeout")


async def test_twilio_idle_monitor_does_not_race_pending_call_end(monkeypatch) -> None:
    monkeypatch.setattr(handler_module, "_CONVERSATION_IDLE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(handler_module, "_CONVERSATION_IDLE_CHECK_INTERVAL_S", 0.01)

    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._running = True
    handler._pending_call_end = True
    handler._last_activity_ts = handler_module.time.monotonic() - 10.0
    handler._terminate_twilio_call = AsyncMock()

    handler._start_idle_monitor()
    handler._last_activity_ts = handler_module.time.monotonic() - 10.0
    await asyncio.wait_for(handler._idle_task, timeout=0.2)

    handler._terminate_twilio_call.assert_not_awaited()


async def test_twilio_stop_cancels_idle_monitor() -> None:
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._running = True
    handler._idle_task = asyncio.create_task(asyncio.sleep(60))

    await handler.stop()

    assert handler._idle_task is None


async def test_twilio_audio_pacer_sends_first_chunk_without_initial_delay() -> None:
    """Latency fix: the pacer must NOT sleep before the first chunk. The old
    loop slept _AUDIO_PACE_MS (250 ms) before dequeuing anything, adding a
    fixed 250 ms to the start of every assistant reply."""
    handler, _cancel = _twilio_handler_with_cancel()
    handler._first_chunk_pending = True

    await handler._enqueue_audio(b"\x7f" * 100)
    # Yield briefly — far less than _AUDIO_PACE_MS — the chunk must already be out.
    await asyncio.sleep(0.05)

    items = _queued_items(handler)
    assert len(items) == 1
    assert handler._protocol.media_payload(items[0]) == b"\x7f" * 100
    assert handler._first_chunk_pending is False
    if handler._pacer_task and not handler._pacer_task.done():
        handler._pacer_task.cancel()


async def test_twilio_audio_pacer_paces_subsequent_chunks() -> None:
    """Steady-state pacing must remain ~realtime (not a burst flood)."""
    handler, _cancel = _twilio_handler_with_cancel()

    await handler._enqueue_audio(b"\x7f" * (handler._AUDIO_CHUNK_SIZE * 3))
    await asyncio.sleep(0.05)

    # Only the first chunk may be out this early; the rest are paced.
    early_items = _queued_items(handler)
    assert len(early_items) == 1
    assert len(handler._audio_accum) == handler._AUDIO_CHUNK_SIZE * 2
    if handler._pacer_task and not handler._pacer_task.done():
        handler._pacer_task.cancel()


async def test_twilio_audio_pacer_drains_full_buffer_over_time() -> None:
    handler, _cancel = _twilio_handler_with_cancel()

    await handler._enqueue_audio(b"\x7f" * (handler._AUDIO_CHUNK_SIZE * 2))
    assert handler._pacer_task is not None
    await asyncio.wait_for(handler._pacer_task, timeout=2.0)

    items = _queued_items(handler)
    assert len(items) == 2
    assert not handler._audio_accum


async def test_twilio_barge_in_still_cancels_pacer_and_clears_audio() -> None:
    """Barge-in must beat pacing: no stale chunk may escape after clear."""
    handler, _cancel = _twilio_handler_with_cancel()

    await handler._enqueue_audio(b"\x7f" * (handler._AUDIO_CHUNK_SIZE * 4))
    await handler._handle_barge_in()

    assert not handler._audio_accum
    assert handler._pacer_task is None
    for item in _queued_items(handler):
        assert item.get("event") != "media"


# ---------------------------------------------------------------------------
# Deterministic governed speech: VoiceLive synthesizes exact pre-generated
# assistant messages inside its server AEC path; the model never authors them.
# ---------------------------------------------------------------------------

def _deterministic_handler(
    *,
    response_create: AsyncMock | None = None,
    response_cancel: AsyncMock | None = None,
) -> TwilioVoiceLiveHandler:
    tts_pool = SimpleNamespace(
        acquire_for_session=AsyncMock(
            side_effect=AssertionError("governed speech must not use external TTS")
        )
    )
    websocket = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(tts_pool=tts_pool)))
    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id="twilio-session-1")
    handler._running = True
    handler._protocol.stream_sid = "MZ123"
    handler._connection = SimpleNamespace(
        response=SimpleNamespace(
            create=response_create or AsyncMock(),
            cancel=response_cancel or AsyncMock(),
        )
    )
    return handler


async def _identify_governed_response(
    handler: TwilioVoiceLiveHandler,
    response_id: str,
    *,
    transcript: str | None = None,
) -> None:
    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id=response_id)),
        ServerEventType.RESPONSE_CREATED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(
            response_id=response_id,
            transcript=transcript or handler._governed_expected_text,
        ),
        ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE,
    )


async def test_twilio_deterministic_speech_uses_exact_voicelive_message_and_keeps_input_enabled() -> None:
    response_create = AsyncMock()
    handler = _deterministic_handler(response_create=response_create)

    class _InputBuffer:
        def __init__(self) -> None:
            self.append = AsyncMock()

    handler._session_opened = True
    handler._connection.input_audio_buffer = _InputBuffer()

    played = await handler._play_deterministic_speech(
        "Thanks for calling. Take care, goodbye.", speech_key="close-user-goodbye"
    )

    assert played is True
    response_create.assert_awaited_once()
    response_params = response_create.await_args.kwargs["response"]
    message = response_params.pre_generated_assistant_message
    assert message.role == "assistant"
    assert message.type == "message"
    assert len(message.content) == 1
    assert message.content[0].type == "text"
    assert message.content[0].text == "Thanks for calling. Take care, goodbye."
    assert response_params.metadata is None
    assert set(response_params.as_dict()) == {"pre_generated_assistant_message"}
    assert "event_id" not in response_create.await_args.kwargs
    handler.websocket.app.state.tts_pool.acquire_for_session.assert_not_awaited()

    await handler._handle_media(
        {
            "event": "media",
            "streamSid": "MZ123",
            "media": {"payload": base64.b64encode(b"\xff" * 160).decode("ascii")},
        }
    )
    handler._connection.input_audio_buffer.append.assert_awaited_once()


async def test_twilio_deterministic_speech_serializes_voicelive_responses() -> None:
    response_create = AsyncMock()
    handler = _deterministic_handler(response_create=response_create)

    assert await handler._play_deterministic_speech("First exact line.", speech_key="first")
    await _identify_governed_response(handler, "resp-first")

    second = asyncio.create_task(
        handler._play_deterministic_speech("Second exact line.", speech_key="second")
    )
    await asyncio.sleep(0)

    assert response_create.await_count == 1
    assert not second.done()

    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-first"),
        ServerEventType.RESPONSE_DONE,
    )

    assert await second is True
    assert response_create.await_count == 2
    second_params = response_create.await_args_list[1].kwargs["response"]
    assert second_params.pre_generated_assistant_message.content[0].text == "Second exact line."


async def test_twilio_deterministic_speech_ignores_unrelated_response_lifecycle() -> None:
    response_create = AsyncMock()
    handler = _deterministic_handler(response_create=response_create)

    assert await handler._play_deterministic_speech("Exact line.", speech_key="governed")

    await _identify_governed_response(
        handler,
        "resp-unrelated",
        transcript="An unrelated assistant response.",
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-unrelated"),
        ServerEventType.RESPONSE_DONE,
    )

    assert handler._governed_response_id is None
    assert not handler._governed_response_done.is_set()

    await _identify_governed_response(handler, "resp-governed")
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-unrelated"),
        ServerEventType.RESPONSE_DONE,
    )
    assert not handler._governed_response_done.is_set()

    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed"),
        ServerEventType.RESPONSE_DONE,
    )
    assert handler._governed_response_done.is_set()


async def test_twilio_deterministic_speech_claims_exact_transcript_prefix() -> None:
    handler = _deterministic_handler()
    exact_line = "This exact governed line is deliberately longer than thirty-two characters."

    assert await handler._play_deterministic_speech(exact_line, speech_key="governed")
    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-governed")),
        ServerEventType.RESPONSE_CREATED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed", delta=exact_line[:32]),
        ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
    )

    assert handler._governed_response_id == "resp-governed"
    assert handler._governed_response_pending is False


async def test_twilio_deterministic_speech_timeout_cancels_and_fails_closed() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    handler._GOVERNED_RESPONSE_TIMEOUT_S = 0.01

    assert await handler._play_deterministic_speech("First exact line.", speech_key="first")
    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-first")),
        ServerEventType.RESPONSE_CREATED,
    )
    handler._outbound_queue.put_nowait(handler._protocol.create_media(b"stale-governed-audio"))
    assert not await handler._play_deterministic_speech("Second exact line.", speech_key="second")

    assert handler._connection.response.create.await_count == 1
    response_cancel.assert_awaited_once()
    assert handler._governed_response_done.is_set()
    assert handler._governed_response_id is None
    assert "resp-first" in handler._interrupted_response_ids
    assert not handler._active_response_ids
    items = _queued_items(handler)
    assert all(item.get("event") != "media" for item in items if item)
    assert {"event": "clear", "streamSid": "MZ123"} in items


async def test_twilio_deterministic_speech_error_event_releases_governed_slot() -> None:
    handler = _deterministic_handler()

    assert await handler._play_deterministic_speech("First exact line.", speech_key="first")
    assert handler._governed_response_pending is True

    # Service-side validation rejection: no response.created will ever arrive.
    await handler._handle_voicelive_event(
        SimpleNamespace(
            error=SimpleNamespace(
                code="invalid_request",
                message="extra fields not permitted",
                type="invalid_request_error",
                param="response.pre_generated_assistant_message",
            )
        ),
        ServerEventType.ERROR,
    )

    assert handler._governed_response_pending is False
    assert handler._governed_response_done.is_set()

    # The next governed line must speak immediately, not wait out the timeout.
    assert await handler._play_deterministic_speech("Second exact line.", speech_key="second")
    assert handler._connection.response.create.await_count == 2


async def test_twilio_error_event_does_not_disturb_claimed_governed_response() -> None:
    handler = _deterministic_handler()

    assert await handler._play_deterministic_speech("Exact line.", speech_key="governed")
    await _identify_governed_response(handler, "resp-governed")

    # An unrelated later error must not release the claimed, still-playing slot.
    await handler._handle_voicelive_event(
        SimpleNamespace(error=SimpleNamespace(code="response_cancel_not_active", message="benign")),
        ServerEventType.ERROR,
    )

    assert handler._governed_response_id == "resp-governed"
    assert not handler._governed_response_done.is_set()


async def test_twilio_unrelated_error_does_not_release_pending_governed_response() -> None:
    handler = _deterministic_handler()

    assert await handler._play_deterministic_speech("Exact line.", speech_key="governed")
    await handler._handle_voicelive_event(
        SimpleNamespace(
            error=SimpleNamespace(
                code="invalid_value",
                message="unrelated session error",
                type="invalid_request_error",
                param="session.voice",
            )
        ),
        ServerEventType.ERROR,
    )

    assert handler._governed_response_pending is True
    assert not handler._governed_response_done.is_set()


async def test_twilio_deterministic_speech_blocks_unclaimed_response_after_timeout() -> None:
    handler = _deterministic_handler()
    handler._GOVERNED_RESPONSE_TIMEOUT_S = 0.01

    assert await handler._play_deterministic_speech("First exact line.", speech_key="first")
    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-unclaimed")),
        ServerEventType.RESPONSE_CREATED,
    )
    assert not await handler._play_deterministic_speech("Second exact line.", speech_key="second")

    assert "resp-unclaimed" in handler._interrupted_response_ids
    assert "resp-unclaimed" not in handler._active_response_ids


async def test_twilio_internal_speech_quarantine_cancels_response() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    handler._active_response_ids.add("resp-unsafe")

    await handler._block_internal_tool_speech("resp-unsafe", reason="test quarantine")

    response_cancel.assert_awaited_once()


async def test_twilio_deterministic_speech_terminal_arms_event_driven_call_end() -> None:
    handler = _deterministic_handler()
    handler._terminate_twilio_call = AsyncMock()

    played = await handler._play_deterministic_speech(
        "Thanks for calling, and take care.",
        speech_key="safety-clinical-close",
        terminal_reason="clinical",
    )

    assert played is True
    assert handler._pending_call_end is True
    assert handler._call_end_reason == "clinical"
    # VoiceLive audio and response completion events, not request acceptance,
    # prove that the close-out reached playout.
    assert handler._call_end_audio_seen is False
    assert handler._call_end_response_done is False
    assert handler._call_end_task is not None
    await handler._call_end_task
    handler._terminate_twilio_call.assert_awaited_once_with("clinical", generation=1)


async def test_twilio_deterministic_terminal_audio_and_done_advance_call_end(monkeypatch) -> None:
    handler = _deterministic_handler()
    handler._terminate_twilio_call = AsyncMock()
    monkeypatch.setattr(
        handler_module,
        "convert_voicelive_delta_to_ulaw",
        lambda _delta: b"governed-sign-off",
    )

    assert await handler._play_deterministic_speech(
        "Thanks for calling, and take care.",
        speech_key="safety-clinical-close",
        terminal_reason="clinical",
    )
    await _identify_governed_response(handler, "resp-governed")
    handler._safe_assistant_response_ids.add("resp-governed")

    await handler._handle_voicelive_event(
        SimpleNamespace(delta="audio", response_id="resp-governed"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )
    assert handler._call_end_audio_seen is True
    assert handler._call_end_response_done is False

    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed"),
        ServerEventType.RESPONSE_DONE,
    )
    assert handler._call_end_response_done is True
    assert handler._governed_response_done.is_set()
    if handler._pacer_task and not handler._pacer_task.done():
        handler._pacer_task.cancel()


async def test_twilio_deterministic_speech_retires_model_audio_before_response() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    handler._active_response_ids.add("resp-model")
    await handler._enqueue_audio(b"\x7f" * (handler._AUDIO_CHUNK_SIZE * 4))
    generation_before = handler._playout_generation

    played = await handler._play_deterministic_speech("Exact line.", speech_key="k")

    assert played is True
    # Stale model audio was retired the same way barge-in retires it.
    assert handler._playout_generation == generation_before + 1
    assert "resp-model" in handler._interrupted_response_ids
    assert not handler._active_response_ids
    if handler._pacer_task is not None:
        await asyncio.wait_for(handler._pacer_task, timeout=2.0)
    items = _queued_items(handler)
    assert any(item and item.get("event") == "clear" for item in items)
    response_cancel.assert_awaited_once()


async def test_twilio_deterministic_speech_retires_completed_local_audio_without_cancel() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    await handler._enqueue_audio(b"\x7f" * (handler._AUDIO_CHUNK_SIZE * 4))

    assert await handler._play_deterministic_speech("Exact line.", speech_key="governed")

    response_cancel.assert_not_awaited()
    if handler._pacer_task is not None:
        await asyncio.wait_for(handler._pacer_task, timeout=2.0)
    queued = _queued_items(handler)
    assert any(item and item.get("event") == "media" for item in queued)
    assert all(not item or item.get("event") != "clear" for item in queued)


async def test_twilio_governed_prefix_echo_does_not_clear_or_reenter_orchestrator() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    exact_line = "I am going to have the clinic team follow up so they can help with that."

    assert await handler._play_deterministic_speech(exact_line, speech_key="safety-question-close")
    await _identify_governed_response(handler, "resp-governed")
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed"),
        ServerEventType.RESPONSE_DONE,
    )

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    assert response_cancel.await_count == 0
    assert all(
        not item or item.get("event") != "clear"
        for item in _queued_items(handler)
    )

    event = SimpleNamespace(item_id="item-echo", transcript="I am going to have")
    await handler._handle_voicelive_event(
        event,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )

    assert not handler._should_forward_event_to_orchestrator(
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
        event,
    )
    response_cancel.assert_not_awaited()


async def test_twilio_item_scoped_governed_echo_decides_on_four_word_delta() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    exact_line = "I am going to have the clinic team follow up so they can help with that."

    assert await handler._play_deterministic_speech(
        exact_line,
        speech_key="safety-question-close",
    )
    await _identify_governed_response(handler, "resp-governed")
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed"),
        ServerEventType.RESPONSE_DONE,
    )

    speech_started = SimpleNamespace(item_id="item-echo")
    await handler._handle_voicelive_event(
        speech_started,
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    delta = SimpleNamespace(item_id="item-echo", delta="I am going to")
    await handler._handle_voicelive_event(
        delta,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA,
    )

    assert not handler._should_forward_event_to_orchestrator(
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA,
        delta,
    )
    response_cancel.assert_not_awaited()
    assert all(
        not item or item.get("event") != "clear"
        for item in _queued_items(handler)
    )

    completed = SimpleNamespace(item_id="item-echo", transcript="I am going to have")
    await handler._handle_voicelive_event(
        completed,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )
    assert not handler._should_forward_event_to_orchestrator(
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
        completed,
    )


async def test_twilio_missing_speech_start_item_binds_first_divergent_delta() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    exact_line = "I am going to have the clinic team follow up so they can help with that."

    assert await handler._play_deterministic_speech(
        exact_line,
        speech_key="safety-question-close",
    )
    await _identify_governed_response(handler, "resp-governed")
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed"),
        ServerEventType.RESPONSE_DONE,
    )

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    delta = SimpleNamespace(item_id="item-bound", delta="What time does the")
    await handler._handle_voicelive_event(
        delta,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA,
    )

    response_cancel.assert_awaited_once()
    assert {"event": "clear", "streamSid": "MZ123"} in _queued_items(handler)


async def test_twilio_completion_before_delta_lets_bound_caller_win_once() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    exact_line = "I am going to have the clinic team follow up so they can help with that."

    assert await handler._play_deterministic_speech(
        exact_line,
        speech_key="safety-question-close",
    )
    await _identify_governed_response(handler, "resp-governed")
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed"),
        ServerEventType.RESPONSE_DONE,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )

    completed = SimpleNamespace(item_id="item-no-delta", transcript="Yes")
    await handler._handle_voicelive_event(
        completed,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )
    await handler._handle_voicelive_event(
        completed,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )

    response_cancel.assert_awaited_once()
    clears = [
        item
        for item in _queued_items(handler)
        if item and item.get("event") == "clear"
    ]
    assert len(clears) == 1


async def test_twilio_completion_without_any_item_id_lets_caller_win() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    governed = "I am going to have the clinic team follow up."
    handler._recent_governed_lines.append((governed, time.monotonic() + 5))
    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    completed = SimpleNamespace(transcript="I am going to have")

    await handler._handle_voicelive_event(
        completed,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )

    assert handler._should_forward_event_to_orchestrator(
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
        completed,
    )
    response_cancel.assert_awaited_once()
    assert {"event": "clear", "streamSid": "MZ123"} in _queued_items(handler)


async def test_twilio_item_barge_deadline_without_delta_clears_once() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    handler._BARGE_DECISION_DEADLINE_S = 0
    handler._recent_governed_lines.append(("Exact governed line.", time.monotonic() + 5))
    speech_started = SimpleNamespace(item_id="item-deadline")

    await handler._handle_voicelive_event(
        speech_started,
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        speech_started,
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    deadline_task = handler._pending_barge_decisions["item-deadline"].deadline_task
    assert deadline_task is not None
    await deadline_task

    response_cancel.assert_awaited_once()
    clears = [
        item
        for item in _queued_items(handler)
        if item and item.get("event") == "clear"
    ]
    assert len(clears) == 1
    assert "item-deadline" not in handler._pending_barge_decisions


async def test_twilio_barge_deadline_default_outlives_live_transcription() -> None:
    """Regression: programme 1c3a2ced staging rollback.

    The 250ms default deadline always expired before live gpt-4o-transcribe
    produced any transcript (observed 2-5s), so every governed echo
    fail-opened to "caller" and sent a spurious clear. The default must
    comfortably exceed live transcription completion latency while staying
    bounded as a leak cap.
    """
    handler = _deterministic_handler(response_cancel=AsyncMock())
    assert handler._BARGE_DECISION_DEADLINE_S >= 6.0
    assert handler._BARGE_DECISION_DEADLINE_S <= 15.0


async def test_twilio_echo_transcript_slower_than_250ms_still_suppresses() -> None:
    """Regression: echo transcript arriving after the old 250ms window.

    Mirrors the failed staging governed_echo probe: the echo transcription
    completes well after 250ms of wall time. With the leak-cap deadline the
    pending decision must still be alive to classify it as echo, so no
    clear and no response.cancel are issued.
    """
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    exact_line = "I am going to have the clinic team follow up so they can help with that."

    assert await handler._play_deterministic_speech(
        exact_line,
        speech_key="safety-question-close",
    )
    await _identify_governed_response(handler, "resp-governed")
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed"),
        ServerEventType.RESPONSE_DONE,
    )

    speech_started = SimpleNamespace(item_id="item-slow-echo")
    await handler._handle_voicelive_event(
        speech_started,
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    # Emulate live STT latency: well past the old 250ms deadline, the
    # decision must still be pending rather than already failed open.
    await asyncio.sleep(0.35)
    assert "item-slow-echo" in handler._pending_barge_decisions

    completed = SimpleNamespace(
        item_id="item-slow-echo",
        transcript="I am going to have the clinic team follow up so they can help",
    )
    await handler._handle_voicelive_event(
        completed,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )

    response_cancel.assert_not_awaited()
    assert all(
        not item or item.get("event") != "clear"
        for item in _queued_items(handler)
    )
    assert handler._resolved_barge_decisions.get("item-slow-echo") == "echo"


async def test_twilio_slow_genuine_caller_still_clears_after_completion() -> None:
    """A short genuine utterance with slow transcription must still barge in."""
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    exact_line = "I am going to have the clinic team follow up so they can help with that."

    assert await handler._play_deterministic_speech(
        exact_line,
        speech_key="safety-question-close",
    )
    await _identify_governed_response(handler, "resp-governed")
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed"),
        ServerEventType.RESPONSE_DONE,
    )

    speech_started = SimpleNamespace(item_id="item-slow-caller")
    await handler._handle_voicelive_event(
        speech_started,
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await asyncio.sleep(0.35)
    assert "item-slow-caller" in handler._pending_barge_decisions

    completed = SimpleNamespace(item_id="item-slow-caller", transcript="Yes please")
    await handler._handle_voicelive_event(
        completed,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )

    response_cancel.assert_awaited_once()
    assert {"event": "clear", "streamSid": "MZ123"} in _queued_items(handler)
    assert handler._resolved_barge_decisions.get("item-slow-caller") == "caller"


async def test_twilio_cleanup_cancels_bound_and_unbound_barge_deadlines() -> None:
    handler = _deterministic_handler(response_cancel=AsyncMock())
    handler._BARGE_DECISION_DEADLINE_S = 60
    handler._recent_governed_lines.append(("Exact governed line.", time.monotonic() + 5))
    await handler._handle_voicelive_event(
        SimpleNamespace(item_id="item-bound"),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    bound_task = handler._pending_barge_decisions["item-bound"].deadline_task
    assert handler._unbound_barge_decision is not None
    unbound_task = handler._unbound_barge_decision.deadline_task

    await handler.stop()

    assert bound_task is not None and bound_task.done()
    assert unbound_task is not None and unbound_task.done()
    assert handler._pending_barge_decisions == {}
    assert handler._unbound_barge_decision is None


async def test_twilio_transcription_failure_before_four_words_clears_once() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    handler._recent_governed_lines.append(("Exact governed line.", time.monotonic() + 5))
    await handler._handle_voicelive_event(
        SimpleNamespace(item_id="item-failed"),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )

    failed = SimpleNamespace(item_id="item-failed")
    await handler._handle_voicelive_event(
        failed,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED,
    )
    await handler._handle_voicelive_event(
        failed,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED,
    )

    response_cancel.assert_awaited_once()
    clears = [
        item
        for item in _queued_items(handler)
        if item and item.get("event") == "clear"
    ]
    assert len(clears) == 1


async def test_twilio_interleaved_item_decisions_do_not_cross_suppress() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    governed = "I am going to have the clinic team follow up."
    handler._recent_governed_lines.append((governed, time.monotonic() + 5))
    for item_id in ("item-echo", "item-caller"):
        await handler._handle_voicelive_event(
            SimpleNamespace(item_id=item_id),
            ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
        )

    echo_delta = SimpleNamespace(item_id="item-echo", delta="I am going to")
    caller_delta = SimpleNamespace(item_id="item-caller", delta="What time does the")
    await handler._handle_voicelive_event(
        echo_delta,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA,
    )
    await handler._handle_voicelive_event(
        caller_delta,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA,
    )
    await handler._handle_voicelive_event(
        echo_delta,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA,
    )
    caller_completed = SimpleNamespace(
        item_id="item-caller",
        transcript="What time does the clinic close?",
    )
    await handler._handle_voicelive_event(
        caller_completed,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )

    assert not handler._should_forward_event_to_orchestrator(
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
        SimpleNamespace(item_id="item-echo", transcript="I am going to have"),
    )
    assert handler._should_forward_event_to_orchestrator(
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
        caller_completed,
    )
    response_cancel.assert_awaited_once()
    clears = [
        item
        for item in _queued_items(handler)
        if item and item.get("event") == "clear"
    ]
    assert len(clears) == 1


async def test_twilio_confirmed_caller_lineage_tags_response_and_media_only() -> None:
    handler = _deterministic_handler(response_cancel=AsyncMock())
    governed = "I am going to have the clinic team follow up."
    handler._recent_governed_lines.append((governed, time.monotonic() + 5))

    await handler._handle_voicelive_event(
        SimpleNamespace(item_id="item-echo"),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(item_id="item-echo", delta="I am going to"),
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA,
    )
    assert handler._caller_turn_lineage == 0

    await handler._handle_voicelive_event(
        SimpleNamespace(item_id="item-caller"),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(item_id="item-caller", delta="What time does the"),
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA,
    )
    assert handler._caller_turn_lineage == 1

    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-caller")),
        ServerEventType.RESPONSE_CREATED,
    )
    assert handler._response_lineages["resp-caller"] == 1

    media = handler._create_media_message(b"lineage-audio")
    assert media["_wulo_caller_turn_lineage"] == 1
    public_media = handler._public_outbound_item(media)
    assert "_wulo_caller_turn_lineage" not in public_media
    assert "_wulo_playout_generation" not in public_media


async def test_twilio_audio_accumulator_keeps_response_lineage(monkeypatch) -> None:
    handler, _cancel = _twilio_handler_with_cancel()
    monkeypatch.setattr(
        handler_module,
        "convert_voicelive_delta_to_ulaw",
        lambda _delta: b"lineage-audio",
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-zero")),
        ServerEventType.RESPONSE_CREATED,
    )
    handler._safe_assistant_response_ids.add("resp-zero")

    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-zero", delta="audio"),
        ServerEventType.RESPONSE_AUDIO_DELTA,
    )

    assert handler._audio_accum_lineage == 0
    if handler._pacer_task is not None:
        await handler._pacer_task
    media = next(
        item
        for item in _queued_items(handler)
        if item and item.get("event") == "media"
    )
    assert media["_wulo_caller_turn_lineage"] == 0


async def test_twilio_new_lineage_response_clears_stale_post_done_media_once() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    handler._outbound_queue.put_nowait(
        handler._create_media_message(b"old-post-done-audio", lineage=0)
    )
    handler._caller_turn_lineage = 1

    created = SimpleNamespace(response=SimpleNamespace(id="resp-new"))
    await handler._handle_voicelive_event(created, ServerEventType.RESPONSE_CREATED)
    await handler._handle_voicelive_event(created, ServerEventType.RESPONSE_CREATED)

    queued = _queued_items(handler)
    clears = [item for item in queued if item and item.get("event") == "clear"]
    assert len(clears) == 1
    assert all(not item or item.get("event") != "media" for item in queued)
    assert handler._response_lineages["resp-new"] == 1
    response_cancel.assert_not_awaited()


async def test_twilio_new_lineage_response_invalidates_stale_active_response() -> None:
    handler = _deterministic_handler(response_cancel=AsyncMock())
    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-old")),
        ServerEventType.RESPONSE_CREATED,
    )
    handler._caller_turn_lineage = 1

    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-new")),
        ServerEventType.RESPONSE_CREATED,
    )

    assert "resp-old" in handler._interrupted_response_ids
    assert "resp-old" not in handler._active_response_ids
    assert "resp-new" in handler._active_response_ids
    assert handler._response_lineages == {"resp-old": 0, "resp-new": 1}


async def test_twilio_same_lineage_response_preserves_queued_continuation() -> None:
    handler = _deterministic_handler(response_cancel=AsyncMock())
    handler._outbound_queue.put_nowait(
        handler._create_media_message(b"same-lineage-audio", lineage=0)
    )

    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-same")),
        ServerEventType.RESPONSE_CREATED,
    )

    queued = _queued_items(handler)
    assert any(item and item.get("event") == "media" for item in queued)
    assert all(not item or item.get("event") != "clear" for item in queued)


async def test_twilio_confirmed_barge_clear_prevents_new_response_duplicate_clear() -> None:
    handler = _deterministic_handler(response_cancel=AsyncMock())
    handler._outbound_queue.put_nowait(
        handler._create_media_message(b"old-lineage-audio", lineage=0)
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(item_id="item-caller"),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-next")),
        ServerEventType.RESPONSE_CREATED,
    )

    queued = _queued_items(handler)
    clears = [item for item in queued if item and item.get("event") == "clear"]
    assert len(clears) == 1
    assert handler._response_lineages["resp-next"] == 1


async def test_twilio_duplicate_speech_started_item_advances_lineage_once() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    speech_started = SimpleNamespace(item_id="item-caller")

    await handler._handle_voicelive_event(
        speech_started,
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    handler._barge_in_duplicate_guard_until = 0
    await handler._handle_voicelive_event(
        speech_started,
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    completed = SimpleNamespace(item_id="item-caller", transcript="Clinic hours please")
    await handler._handle_voicelive_event(
        completed,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )

    assert handler._caller_turn_lineage == 1
    assert handler._resolved_barge_decisions == {"item-caller": "caller"}
    response_cancel.assert_awaited_once()
    clears = [
        item
        for item in _queued_items(handler)
        if item and item.get("event") == "clear"
    ]
    assert len(clears) == 1


async def test_twilio_genuine_turn_during_recent_governed_line_still_interrupts() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)
    exact_line = "I am going to have the clinic team follow up so they can help with that."

    assert await handler._play_deterministic_speech(exact_line, speech_key="safety-question-close")
    await _identify_governed_response(handler, "resp-governed")
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed"),
        ServerEventType.RESPONSE_DONE,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )

    event = SimpleNamespace(transcript="What time does the clinic close?")
    await handler._handle_voicelive_event(
        event,
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )

    assert handler._should_forward_event_to_orchestrator(
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED
    )
    response_cancel.assert_awaited_once()
    assert {"event": "clear", "streamSid": "MZ123"} in _queued_items(handler)


async def test_twilio_deterministic_speech_barge_in_clears_and_cancels() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)

    assert await handler._play_deterministic_speech("Exact line.", speech_key="governed")
    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-governed")),
        ServerEventType.RESPONSE_CREATED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed", delta="Exact "),
        ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
    )
    handler._outbound_queue.put_nowait(handler._protocol.create_media(b"governed-audio"))
    second = asyncio.create_task(
        handler._play_deterministic_speech("Next exact line.", speech_key="next")
    )
    await asyncio.sleep(0)

    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(transcript="Please stop and listen to me."),
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )

    assert {"event": "clear", "streamSid": "MZ123"} in _queued_items(handler)
    assert "resp-governed" in handler._interrupted_response_ids
    assert not second.done()
    assert handler._connection.response.create.await_count == 1
    response_cancel.assert_awaited_once()

    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-governed"),
        ServerEventType.RESPONSE_DONE,
    )
    assert await second is True
    assert handler._connection.response.create.await_count == 2


async def test_twilio_governed_response_created_after_barge_in_stays_suppressed() -> None:
    response_cancel = AsyncMock()
    handler = _deterministic_handler(response_cancel=response_cancel)

    assert await handler._play_deterministic_speech(
        "This exact governed line is long enough to continue after a short interruption.",
        speech_key="governed",
    )

    # Speech starts after response.create was sent but before response.created.
    await handler._handle_voicelive_event(
        SimpleNamespace(),
        ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(response=SimpleNamespace(id="resp-late")),
        ServerEventType.RESPONSE_CREATED,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(transcript="Please stop and listen to me."),
        ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    )

    assert "resp-late" in handler._interrupted_response_ids

    # A very short user turn releases time-based suppression, but the late
    # response ID remains explicitly interrupted until its own response.done.
    handler._release_barge_in_suppression_after_user_turn()
    assert handler._should_drop_interrupted_audio("resp-late", now=time.perf_counter())

    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-late", delta="This exact "),
        ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
    )
    await handler._handle_voicelive_event(
        SimpleNamespace(response_id="resp-late"),
        ServerEventType.RESPONSE_DONE,
    )

    assert handler._governed_response_done.is_set()
    assert "resp-late" not in handler._interrupted_response_ids


async def test_twilio_deterministic_speech_fails_closed_without_external_tts_fallback() -> None:
    response_create = AsyncMock(side_effect=RuntimeError("VoiceLive unavailable"))
    handler = _deterministic_handler(response_create=response_create)

    played = await handler._play_deterministic_speech("Exact line.", speech_key="governed")

    assert played is False
    assert handler._governed_response_done.is_set()
    assert handler._outbound_queue.empty()
    handler.websocket.app.state.tts_pool.acquire_for_session.assert_not_awaited()


async def test_twilio_deterministic_speech_fails_closed_without_connection() -> None:
    handler = TwilioVoiceLiveHandler(websocket=object(), session_id="twilio-session-1")
    handler._running = True
    handler._protocol.stream_sid = "MZ123"

    played = await handler._play_deterministic_speech("Exact line.", speech_key="k")

    assert played is False
    assert handler._pending_call_end is False
    assert handler._outbound_queue.empty()


async def test_twilio_messenger_exposes_deterministic_speech_capability() -> None:
    handler = _deterministic_handler()

    played = await handler._messenger.play_deterministic_speech(
        "Exact line.", speech_key="k", terminal_reason=None
    )

    assert played is True
    handler._connection.response.create.assert_awaited_once()