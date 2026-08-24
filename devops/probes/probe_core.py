"""Privacy-minimized Twilio media-stream mechanics for synthetic probes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any, Protocol

from defusedxml import ElementTree as ET
from websockets.exceptions import ConnectionClosed

FRAME_BYTES = 160
FRAME_INTERVAL_SECONDS = 0.02
MU_LAW_SILENCE = b"\xff" * FRAME_BYTES
MARK_ECHO_PADDING_SECONDS = 0.25


class Clock(Protocol):
    """Minimal clock contract used by real probes and deterministic tests."""

    def monotonic(self) -> float: ...

    async def sleep(self, delay: float) -> None: ...


class RealClock:
    def monotonic(self) -> float:
        return time.perf_counter()

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


def twilio_signature(url: str, params: Mapping[str, str], auth_token: str) -> str:
    """Return Twilio's HMAC-SHA1 signature for a form-encoded webhook."""
    payload = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def extract_stream_parameters(twiml: str) -> dict[str, str]:
    """Copy every TwiML Stream Parameter into a bounded start-frame mapping."""
    root = ET.fromstring(twiml)
    parameters: dict[str, str] = {}
    for element in root.iter("Parameter"):
        name = element.attrib.get("name", "")
        if not name or name in parameters:
            raise ValueError("TwiML stream parameters require unique non-empty names")
        parameters[name] = element.attrib.get("value", "")
    return parameters


def build_start_frame(
    *,
    account_sid: str,
    call_sid: str,
    stream_sid: str,
    custom_parameters: Mapping[str, str],
) -> dict[str, Any]:
    """Build the realistic Twilio media-stream start event."""
    return {
        "event": "start",
        "sequenceNumber": "1",
        "start": {
            "accountSid": account_sid,
            "callSid": call_sid,
            "streamSid": stream_sid,
            "tracks": ["inbound"],
            "mediaFormat": {
                "encoding": "audio/x-mulaw",
                "sampleRate": 8000,
                "channels": 1,
            },
            "customParameters": dict(custom_parameters),
        },
        "streamSid": stream_sid,
    }


class ProbeSession:
    """Faithful Twilio feeder/reader with virtual carrier playout state."""

    def __init__(
        self,
        websocket: Any,
        *,
        account_sid: str,
        call_sid: str,
        stream_sid: str,
        clock: Clock | None = None,
    ) -> None:
        self.websocket = websocket
        self.account_sid = account_sid
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self.clock = clock or RealClock()
        self.sequence = 2
        self.media_frames_received = 0
        self.clear_count = 0
        self.close_code: int | None = None
        self.first_media_at: float | None = None
        self.last_media_at: float | None = None
        self.mark_playback_delay_ms: float | None = None
        self.playback_end = self.clock.monotonic()
        self.event_ledger: list[str] = []
        self.closed = asyncio.Event()
        self._speech: bytes | None = None
        self._speech_position = 0
        self._speech_done = asyncio.Event()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._mark_tasks: set[asyncio.Task[None]] = set()

    @property
    def background_task_count(self) -> int:
        return len(self._background_tasks | self._mark_tasks)

    async def send_start(self, custom_parameters: Mapping[str, str]) -> None:
        await self.websocket.send(
            json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        )
        await self.websocket.send(
            json.dumps(
                build_start_frame(
                    account_sid=self.account_sid,
                    call_sid=self.call_sid,
                    stream_sid=self.stream_sid,
                    custom_parameters=custom_parameters,
                )
            )
        )

    def start_background_tasks(self) -> None:
        if self._background_tasks:
            raise RuntimeError("probe background tasks already started")
        self._track_background(asyncio.create_task(self._feeder()))
        self._track_background(asyncio.create_task(self._reader()))

    async def stop_background_tasks(self) -> None:
        self.closed.set()
        tasks = list(self._background_tasks | self._mark_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._mark_tasks.clear()

    def speak(self, audio: bytes) -> asyncio.Event:
        if not audio:
            raise ValueError("probe speech must not be empty")
        if self._speech is not None:
            raise RuntimeError("probe caller speech is already active")
        self._speech_done = asyncio.Event()
        self._speech = bytes(audio)
        self._speech_position = 0
        return self._speech_done

    async def wait_for_new_audio(self, *, after_count: int, timeout: float) -> None:
        await self._wait_until(
            lambda: self.media_frames_received > after_count,
            timeout=timeout,
            failure="agent_audio_not_started",
        )

    async def wait_for_clear(self, *, after_count: int, timeout: float) -> None:
        await self._wait_until(
            lambda: self.clear_count > after_count,
            timeout=timeout,
            failure="twilio_clear_not_received",
        )

    async def wait_for_audio_idle(
        self, *, after_count: int, gap_seconds: float, timeout: float
    ) -> None:
        await self._wait_until(
            lambda: (
                self.media_frames_received > after_count
                and self.last_media_at is not None
                and self.clock.monotonic() - self.last_media_at >= gap_seconds
            ),
            timeout=timeout,
            failure="agent_audio_not_idle",
        )

    async def _wait_until(self, predicate: Any, *, timeout: float, failure: str) -> None:
        deadline = self.clock.monotonic() + timeout
        while not predicate():
            if self.closed.is_set() or self.clock.monotonic() >= deadline:
                raise TimeoutError(failure)
            await self.clock.sleep(0.05)

    def _track_background(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _track_mark(self, task: asyncio.Task[None]) -> None:
        self._mark_tasks.add(task)
        task.add_done_callback(self._mark_tasks.discard)

    async def _feeder(self) -> None:
        next_frame_at = self.clock.monotonic()
        while not self.closed.is_set():
            payload = self._next_inbound_payload()
            message = {
                "event": "media",
                "sequenceNumber": str(self.sequence),
                "media": {
                    "track": "inbound",
                    "chunk": str(self.sequence),
                    "timestamp": str(self.sequence * 20),
                    "payload": base64.b64encode(payload).decode("ascii"),
                },
                "streamSid": self.stream_sid,
            }
            try:
                await self.websocket.send(json.dumps(message))
            except ConnectionClosed:
                return
            self.sequence += 1
            next_frame_at += FRAME_INTERVAL_SECONDS
            await self.clock.sleep(max(0.0, next_frame_at - self.clock.monotonic()))

    def _next_inbound_payload(self) -> bytes:
        if self._speech is None:
            return MU_LAW_SILENCE
        payload = self._speech[self._speech_position : self._speech_position + FRAME_BYTES]
        self._speech_position += FRAME_BYTES
        if self._speech_position >= len(self._speech):
            self._speech = None
            self._speech_position = 0
            self._speech_done.set()
        return payload.ljust(FRAME_BYTES, b"\xff")

    async def _reader(self) -> None:
        try:
            async for raw_message in self.websocket:
                message = json.loads(raw_message)
                event = message.get("event")
                now = self.clock.monotonic()
                if event == "media":
                    payload = base64.b64decode(message["media"]["payload"], validate=True)
                    self.media_frames_received += 1
                    if self.first_media_at is None:
                        self.first_media_at = now
                    self.last_media_at = now
                    self.playback_end = max(self.playback_end, now) + len(payload) / 8000.0
                    self.event_ledger.append("media_received")
                elif event == "mark":
                    mark_name = str((message.get("mark") or {}).get("name") or "")
                    if not mark_name:
                        continue
                    self.mark_playback_delay_ms = round(
                        max(0.0, self.playback_end - now) * 1000.0,
                        1,
                    )
                    self.event_ledger.append("mark_received")
                    self._track_mark(asyncio.create_task(self._echo_mark(mark_name)))
                elif event == "clear":
                    self.clear_count += 1
                    self.playback_end = now
                    self.event_ledger.append("clear_received")
        except ConnectionClosed:
            pass
        finally:
            self.close_code = getattr(self.websocket, "close_code", None)
            self.event_ledger.append("server_closed")
            self.closed.set()

    async def _echo_mark(self, mark_name: str) -> None:
        delay = max(0.0, self.playback_end - self.clock.monotonic())
        await self.clock.sleep(delay + MARK_ECHO_PADDING_SECONDS)
        try:
            await self.websocket.send(
                json.dumps(
                    {
                        "event": "mark",
                        "streamSid": self.stream_sid,
                        "mark": {"name": mark_name},
                    }
                )
            )
        except ConnectionClosed:
            return
        self.event_ledger.append("mark_echoed")
