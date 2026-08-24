"""Behavior tests for the virtual clock, the fakes, and the scenario detectors."""

import asyncio
import base64

import pytest

from voicekit.clock import VirtualClock
from voicekit.fakes.twilio_media_streams import FakeTwilioMediaStream
from voicekit.fakes.voicelive import FakeVoiceLiveConnection
from voicekit.scenarios import (
    ScenarioFailure,
    assert_first_audio_within,
    assert_no_concurrent_responses,
    assert_no_stale_cancel,
    assert_ordering,
    assert_silence_after_clear,
)


def _media(n_bytes: int) -> dict:
    return {"event": "media", "media": {"payload": base64.b64encode(b"\x00" * n_bytes).decode()}}


# -- VirtualClock ----------------------------------------------------------

async def test_clock_orders_sleepers_deterministically() -> None:
    clock = VirtualClock()
    order: list[str] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        order.append(name)

    tasks = [asyncio.ensure_future(sleeper("b", 0.2)), asyncio.ensure_future(sleeper("a", 0.1))]
    await clock.advance(0.3)
    await asyncio.gather(*tasks)
    assert order == ["a", "b"]
    assert clock.now == pytest.approx(0.3)


async def test_clock_rejects_negative_advance() -> None:
    with pytest.raises(ValueError):
        await VirtualClock().advance(-1)


# -- VoiceLive fake ---------------------------------------------------------

async def test_signature_trap_raises_typeerror_like_the_real_sdk() -> None:
    conn = FakeVoiceLiveConnection()
    with pytest.raises(TypeError):
        await conn.response.create(instructions="say the safety line")  # type: ignore[call-arg]


async def test_create_and_cancel_track_response_ids() -> None:
    conn = FakeVoiceLiveConnection()
    await conn.response.create(additional_instructions="hello")
    assert conn.response.active_response_ids == ["resp_1"]
    await conn.response.cancel(response_id="resp_1")
    assert conn.response.active_response_ids == []
    assert conn.timeline.kinds() == ["response.create", "response.cancel"]


async def test_scripted_create_failure() -> None:
    conn = FakeVoiceLiveConnection()
    conn.response.fail_next_create(RuntimeError("Conversation already has an active response"))
    with pytest.raises(RuntimeError):
        await conn.response.create(additional_instructions="x")
    await conn.response.create(additional_instructions="x")  # next attempt succeeds
    assert len(conn.response.created) == 1


# -- Scenario detectors -------------------------------------------------------

async def test_concurrent_responses_detected() -> None:
    conn = FakeVoiceLiveConnection()
    await conn.response.create()
    await conn.response.create()
    with pytest.raises(ScenarioFailure, match="response race"):
        assert_no_concurrent_responses(conn)


async def test_stale_cancel_detected_and_fresh_cancel_allowed() -> None:
    conn = FakeVoiceLiveConnection()
    await conn.response.create()
    conn.response.complete_response("resp_1")
    await conn.response.cancel(response_id="resp_1")
    with pytest.raises(ScenarioFailure, match="stale cancel"):
        assert_no_stale_cancel(conn)

    fresh = FakeVoiceLiveConnection()
    await fresh.response.create()
    await fresh.response.cancel(response_id="resp_1")  # still in flight: fine
    assert_no_stale_cancel(fresh)


async def test_ordering_detector() -> None:
    conn = FakeVoiceLiveConnection()
    await conn.response.create(additional_instructions="spoke too soon")
    conn.mark_event("safety_gate.done")
    with pytest.raises(ScenarioFailure, match="ordering violation"):
        assert_ordering(conn, gate="safety_gate.done")


# -- Twilio stream fake -------------------------------------------------------

async def test_first_media_delay_and_dead_air(virtual_clock: VirtualClock) -> None:
    stream = FakeTwilioMediaStream(virtual_clock)
    stream.set_trigger()
    with pytest.raises(ScenarioFailure, match="dead air"):
        assert_first_audio_within(stream, max_ms=100)

    await virtual_clock.advance(0.05)
    await stream.send(_media(2000))
    assert stream.first_media_delay_ms() == pytest.approx(50.0)
    assert_first_audio_within(stream, max_ms=100)
    with pytest.raises(ScenarioFailure, match="delayed first audio"):
        assert_first_audio_within(stream, max_ms=10)


async def test_audio_after_clear_detected(virtual_clock: VirtualClock) -> None:
    stream = FakeTwilioMediaStream(virtual_clock)
    await stream.send(_media(2000))
    await stream.send_clear()
    await virtual_clock.advance(0.1)
    await stream.send(_media(2000))
    with pytest.raises(ScenarioFailure, match="stale audio after barge-in"):
        assert_silence_after_clear(stream, grace_ms=20)

    clean = FakeTwilioMediaStream(virtual_clock)
    await clean.send(_media(2000))
    await clean.send_clear()
    assert_silence_after_clear(clean, grace_ms=20)


async def test_unknown_event_rejected(virtual_clock: VirtualClock) -> None:
    stream = FakeTwilioMediaStream(virtual_clock)
    with pytest.raises(ValueError, match="unknown Twilio"):
        await stream.send({"event": "bogus"})
