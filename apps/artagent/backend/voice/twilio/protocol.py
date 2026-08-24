"""Twilio Media Streams protocol helpers."""

from __future__ import annotations

import base64
import json
from typing import Any

from utils.ml_logging import get_logger

logger = get_logger("twilio.protocol")

EVENT_CONNECTED = "connected"
EVENT_START = "start"
EVENT_MEDIA = "media"
EVENT_DTMF = "dtmf"
EVENT_MARK = "mark"
EVENT_STOP = "stop"
TWILIO_MEDIA_ENCODING = "audio/x-mulaw"
TWILIO_MEDIA_SAMPLE_RATE = 8000
TWILIO_MEDIA_CHANNELS = 1


class TwilioProtocol:
    """Tracks Twilio stream state and constructs outbound media messages."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self.account_sid: str | None = None
        self.custom_parameters: dict[str, str] = {}

    def parse_message(self, raw: str) -> dict[str, Any] | None:
        """Parse one Twilio JSON event."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[TwilioProtocol] Failed to parse JSON message")
            return None
        if not isinstance(msg, dict):
            return None
        return msg

    def process_start(self, msg: dict[str, Any]) -> None:
        """Capture stream identifiers and custom parameters from Twilio's start event."""
        start = msg.get("start") if isinstance(msg.get("start"), dict) else {}
        media_format = (
            start.get("mediaFormat")
            if isinstance(start.get("mediaFormat"), dict)
            else {}
        )
        if media_format != {
            "encoding": TWILIO_MEDIA_ENCODING,
            "sampleRate": TWILIO_MEDIA_SAMPLE_RATE,
            "channels": TWILIO_MEDIA_CHANNELS,
        }:
            raise ValueError("unsupported Twilio media format")
        self.stream_sid = str(start.get("streamSid") or msg.get("streamSid") or "") or None
        self.call_sid = str(start.get("callSid") or "") or None
        self.account_sid = str(start.get("accountSid") or "") or None
        raw_params = start.get("customParameters") or {}
        self.custom_parameters = {
            str(key): str(value) for key, value in raw_params.items()
        } if isinstance(raw_params, dict) else {}
        if self.custom_parameters.get("session_id"):
            self.session_id = self.custom_parameters["session_id"]
        logger.info(
            "[TwilioProtocol] Stream started | call=%s stream=%s session=%s params=%s",
            self.call_sid,
            self.stream_sid,
            self.session_id,
            sorted(self.custom_parameters),
        )

    def media_payload(self, msg: dict[str, Any]) -> bytes | None:
        """Decode one inbound Twilio μ-law media payload."""
        media = msg.get("media") if isinstance(msg.get("media"), dict) else {}
        payload = media.get("payload")
        if not payload:
            return None
        try:
            return base64.b64decode(str(payload))
        except Exception:
            logger.warning("[TwilioProtocol] Invalid base64 media payload")
            return None

    def dtmf_digit(self, msg: dict[str, Any]) -> str | None:
        """Extract a DTMF digit from Twilio's dtmf event."""
        dtmf = msg.get("dtmf") if isinstance(msg.get("dtmf"), dict) else {}
        digit = dtmf.get("digit")
        return str(digit) if digit else None

    def create_media(self, audio_bytes: bytes) -> dict[str, Any]:
        """Create an outbound Twilio media event from μ-law bytes."""
        return {
            "event": EVENT_MEDIA,
            "streamSid": self.stream_sid,
            "media": {"payload": base64.b64encode(audio_bytes).decode("ascii")},
        }

    def create_clear(self) -> dict[str, Any]:
        """Create a Twilio clear event to cancel queued playback."""
        return {"event": "clear", "streamSid": self.stream_sid}

    def create_mark(self, name: str) -> dict[str, Any]:
        """Create a Twilio mark event for playback acknowledgements."""
        return {"event": EVENT_MARK, "streamSid": self.stream_sid, "mark": {"name": name}}