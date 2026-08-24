"""Eval corpus: paired good/bad reference agents for each production failure mode.

Every case encodes a real incident. The BAD agent reproduces the bug; the GOOD
agent is the corrected implementation. The eval gate requires every detector to
FAIL the bad agent and PASS the good agent — 0 false negatives, 0 false
positives — before a release ships.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from voicekit.clock import VirtualClock
from voicekit.fakes.twilio_media_streams import MULAW_BYTES_PER_SECOND, FakeTwilioMediaStream
from voicekit.fakes.voicelive import FakeVoiceLiveConnection
from voicekit.scenarios import (
    assert_first_audio_within,
    assert_no_concurrent_responses,
    assert_no_stale_cancel,
    assert_ordering,
    assert_silence_after_clear,
)

SAFETY_LINE = "I can't help with clinical symptoms on this call. I've flagged this for the clinic team."


@dataclass(frozen=True)
class Case:
    id: str
    failure_mode: str
    bad: Callable[[], Awaitable[None]]
    good: Callable[[], Awaitable[None]]


async def _run_with_clock(clock: VirtualClock, coro: Awaitable[None], *, horizon_s: float = 10.0) -> None:
    """Drive a virtual-clock-sleeping coroutine to completion deterministically."""
    task = asyncio.ensure_future(coro)
    await clock.advance(0)
    elapsed = 0.0
    while not task.done() and elapsed < horizon_s:
        await clock.advance(0.05)
        elapsed += 0.05
    if not task.done():
        task.cancel()
        raise AssertionError(f"reference agent did not finish within {horizon_s}s of virtual time")
    await task


# --------------------------------------------------------------------------
# C1 — SDK signature mismatch (the 7-day dead-air bug)
# --------------------------------------------------------------------------

async def c1_bad() -> None:
    """Calls create(instructions=...) — the real SDK only accepts additional_instructions."""
    conn = FakeVoiceLiveConnection()
    await conn.response.create(instructions=SAFETY_LINE)  # type: ignore[call-arg]


async def c1_good() -> None:
    conn = FakeVoiceLiveConnection()
    await conn.response.create(additional_instructions=SAFETY_LINE)
    assert conn.response.created == [{"additional_instructions": SAFETY_LINE}]


# --------------------------------------------------------------------------
# C2 — concurrent response.create race
# --------------------------------------------------------------------------

async def c2_bad() -> None:
    """Two safety escalations fire create() concurrently with no serialization."""
    conn = FakeVoiceLiveConnection()

    async def escalate() -> None:
        await conn.response.create(additional_instructions=SAFETY_LINE)

    await asyncio.gather(escalate(), escalate())
    assert_no_concurrent_responses(conn)


async def c2_good() -> None:
    """Escalations are serialized behind a lock and coalesced while in flight."""
    conn = FakeVoiceLiveConnection()
    lock = asyncio.Lock()
    inflight = False

    async def escalate() -> None:
        nonlocal inflight
        async with lock:
            if inflight:
                return
            await conn.response.create(additional_instructions=SAFETY_LINE)
            inflight = True

    await asyncio.gather(escalate(), escalate())
    conn.response.complete_response()
    assert_no_concurrent_responses(conn)
    assert len(conn.response.created) == 1


# --------------------------------------------------------------------------
# C3 — stale cancel after barge-in
# --------------------------------------------------------------------------

async def c3_bad() -> None:
    """Barge-in handler cancels 'whatever is active' after the response already completed."""
    conn = FakeVoiceLiveConnection()
    await conn.response.create(additional_instructions="First reply")
    conn.response.complete_response("resp_1")  # server: response.done
    await conn.response.cancel()  # late barge-in cancel, no target tracking
    assert_no_stale_cancel(conn)


async def c3_good() -> None:
    """Handler tracks the in-flight response id and skips the cancel when it is done."""
    conn = FakeVoiceLiveConnection()
    await conn.response.create(additional_instructions="First reply")
    inflight = conn.response.active_response_ids[-1]
    conn.response.complete_response(inflight)
    if inflight in conn.response.active_response_ids:  # it is not — so no cancel
        await conn.response.cancel(response_id=inflight)
    assert_no_stale_cancel(conn)


# --------------------------------------------------------------------------
# C4 — response before deterministic gate completed
# --------------------------------------------------------------------------

async def c4_bad() -> None:
    """Agent speaks first, then runs the safety gate / booking writes."""
    conn = FakeVoiceLiveConnection()
    await conn.response.create(additional_instructions=SAFETY_LINE)
    conn.mark_event("safety_gate.done", escalated=True)
    assert_ordering(conn, gate="safety_gate.done")


async def c4_good() -> None:
    """Gate completes (escalation + booking writes awaited) before the response."""
    conn = FakeVoiceLiveConnection()
    conn.mark_event("safety_gate.done", escalated=True)
    await conn.response.create(additional_instructions=SAFETY_LINE)
    assert_ordering(conn, gate="safety_gate.done")


# --------------------------------------------------------------------------
# C5 — dead air / delayed first audio (pacer regression)
# --------------------------------------------------------------------------

def _chunk(n_bytes: int) -> dict[str, Any]:
    import base64

    return {"event": "media", "media": {"payload": base64.b64encode(b"\x7f" * n_bytes).decode()}}


async def c5_bad() -> None:
    """Pacer sleeps a full pace interval BEFORE dequeuing the first chunk (+250ms/reply)."""
    clock = VirtualClock()
    stream = FakeTwilioMediaStream(clock)
    stream.set_trigger()

    async def agent() -> None:
        for _ in range(3):
            await clock.sleep(0.25)  # sleep-before-first-dequeue bug
            await stream.send(_chunk(2000))  # 2000 B = 250 ms of u-law audio

    await _run_with_clock(clock, agent())
    assert_first_audio_within(stream, max_ms=100.0)


async def c5_good() -> None:
    """First available chunk is sent immediately; pacing applies between chunks only."""
    clock = VirtualClock()
    stream = FakeTwilioMediaStream(clock)
    stream.set_trigger()

    async def agent() -> None:
        await stream.send(_chunk(2000))  # first chunk: immediately
        for _ in range(2):
            await clock.sleep(0.25)  # proportional pacing between chunks
            await stream.send(_chunk(2000))

    await _run_with_clock(clock, agent())
    assert_first_audio_within(stream, max_ms=100.0)
    assert stream.total_payload_bytes() == 6000
    assert MULAW_BYTES_PER_SECOND == 8000


# --------------------------------------------------------------------------
# C6 — stale audio after barge-in clear (companion to C3/C5)
# --------------------------------------------------------------------------

async def c6_bad() -> None:
    """Pacer keeps flushing buffered audio after the caller barged in."""
    clock = VirtualClock()
    stream = FakeTwilioMediaStream(clock)
    await stream.send(_chunk(2000))
    await stream.send_clear()  # barge-in

    async def agent() -> None:
        await clock.sleep(0.1)
        await stream.send(_chunk(2000))  # stale buffered audio still flushed

    await _run_with_clock(clock, agent())
    assert_silence_after_clear(stream, grace_ms=20.0)


async def c6_good() -> None:
    clock = VirtualClock()
    stream = FakeTwilioMediaStream(clock)
    await stream.send(_chunk(2000))
    await stream.send_clear()
    # corrected pacer: stops and drops its buffer on clear — nothing more is sent
    assert_silence_after_clear(stream, grace_ms=20.0)


CASES: list[Case] = [
    Case("C1", "SDK signature mismatch (dead-air TypeError)", c1_bad, c1_good),
    Case("C2", "concurrent response.create race", c2_bad, c2_good),
    Case("C3", "stale cancel after barge-in", c3_bad, c3_good),
    Case("C4", "response before deterministic gate", c4_bad, c4_good),
    Case("C5", "dead air / delayed first audio", c5_bad, c5_good),
    Case("C6", "stale audio after barge-in clear", c6_bad, c6_good),
]
