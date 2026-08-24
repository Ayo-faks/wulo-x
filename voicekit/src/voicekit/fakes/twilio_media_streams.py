"""Fake far-end for Twilio Media Streams with a deterministic pacing ledger.

The fake plays the role of Twilio's websocket: your code under test *sends*
protocol messages (``media``/``mark``/``clear``) to it. Every message is
timestamped on a :class:`~voicekit.clock.VirtualClock`, giving deterministic
answers to the questions that matter for perceived latency:

- How long after the trigger was the FIRST audio chunk sent? (dead air)
- Did audio keep flowing after a ``clear`` (barge-in) was issued? (stale audio)
- Does throughput respect the u-law byte budget? (pacer regressions)
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from voicekit.clock import VirtualClock

MULAW_BYTES_PER_SECOND = 8000
"""G.711 u-law at 8 kHz mono: 8000 bytes of payload per second of audio."""


@dataclass(frozen=True)
class MediaChunk:
    at: float
    payload_bytes: int
    after_clear: bool

    @property
    def audio_ms(self) -> float:
        return self.payload_bytes / MULAW_BYTES_PER_SECOND * 1000.0


class FakeTwilioMediaStream:
    """Records outbound Media Streams protocol traffic from the code under test."""

    def __init__(self, clock: VirtualClock, stream_sid: str = "MZfake") -> None:
        self.clock = clock
        self.stream_sid = stream_sid
        self.media: list[MediaChunk] = []
        self.marks: list[tuple[float, str]] = []
        self.clears: list[float] = []
        self.raw_messages: list[dict[str, Any]] = []
        self.trigger_at: float | None = None

    def set_trigger(self) -> None:
        """Mark 'now' as the moment audio became due (e.g. user stopped speaking)."""
        self.trigger_at = self.clock.now

    async def send(self, message: str | dict[str, Any]) -> None:
        """Accept one Twilio Media Streams protocol message (JSON str or dict)."""
        parsed: dict[str, Any] = json.loads(message) if isinstance(message, str) else message
        self.raw_messages.append(parsed)
        event = parsed.get("event")
        now = self.clock.now
        if event == "media":
            payload = parsed.get("media", {}).get("payload", "")
            size = len(base64.b64decode(payload)) if payload else 0
            self.media.append(MediaChunk(at=now, payload_bytes=size, after_clear=bool(self.clears)))
        elif event == "mark":
            self.marks.append((now, parsed.get("mark", {}).get("name", "")))
        elif event == "clear":
            self.clears.append(now)
        else:
            raise ValueError(f"unknown Twilio Media Streams event: {event!r}")

    async def send_clear(self) -> None:
        """Issue a barge-in ``clear`` from the test side (as the handler would)."""
        await self.send({"event": "clear", "streamSid": self.stream_sid})

    # -- Ledger queries -----------------------------------------------------

    @property
    def first_media_at(self) -> float | None:
        return self.media[0].at if self.media else None

    def first_media_delay_ms(self, *, since: float | None = None) -> float | None:
        """Delay from ``since`` (default: the trigger) to the first audio chunk."""
        start = since if since is not None else self.trigger_at
        if start is None:
            raise ValueError("no reference point: call set_trigger() or pass since=")
        if self.first_media_at is None:
            return None
        return (self.first_media_at - start) * 1000.0

    @property
    def media_after_clear(self) -> list[MediaChunk]:
        if not self.clears:
            return []
        first_clear = self.clears[0]
        return [c for c in self.media if c.at >= first_clear and c.after_clear]

    def total_payload_bytes(self) -> int:
        return sum(c.payload_bytes for c in self.media)
