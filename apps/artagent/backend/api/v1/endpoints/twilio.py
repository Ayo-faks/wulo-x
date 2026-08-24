"""Twilio Media Streams WebSocket endpoint."""

from __future__ import annotations

import uuid

from apps.artagent.backend.voice.twilio.handler import TwilioVoiceLiveHandler
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from utils.ml_logging import get_logger

logger = get_logger("api.v1.endpoints.twilio")
router = APIRouter(tags=["Twilio Media Streams"])


@router.get("/health")
async def twilio_health() -> dict[str, str]:
    """Health check for Twilio Media Streams."""
    return {"status": "ok", "service": "twilio-media-streams"}


@router.websocket("/stream")
async def twilio_media_stream(websocket: WebSocket) -> None:
    """Bridge Twilio Media Streams to the ART VoiceLive Recall agent."""
    session_id = websocket.query_params.get("session_id", f"twilio-session-{uuid.uuid4().hex}")
    logger.info("[Twilio] WebSocket connect | session=%s", session_id)

    handler = TwilioVoiceLiveHandler(websocket=websocket, session_id=session_id)
    await websocket.accept()

    try:
        await handler.start()
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if "text" in message:
                await handler.handle_text_message(message["text"])
    except WebSocketDisconnect:
        logger.info("[Twilio] Client disconnected | session=%s", session_id)
    except Exception:
        logger.exception("[Twilio] WebSocket error | session=%s", session_id)
    finally:
        await handler.stop()
        logger.info("[Twilio] WebSocket closed | session=%s", session_id)