"""Twilio Media Streams to Azure VoiceLive bridge."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

import httpx
from apps.artagent.backend.registries.agentstore.loader import build_handoff_map, discover_agents
from apps.artagent.backend.src.orchestration.session_agents import get_session_agent
from apps.artagent.backend.voice.genesys.audio_codec import (
    convert_voicelive_delta_to_ulaw,
    ulaw_8khz_to_pcm16_24khz_b64,
    ulaw_decode,
)
from apps.artagent.backend.voice.shared import DEFAULT_START_AGENT, resolve_orchestrator_config
from apps.artagent.backend.voice.voicelive.credentials import (
    get_voicelive_credential,
)
from apps.artagent.backend.voice.voicelive.orchestrator import (
    LiveOrchestrator,
    register_voicelive_orchestrator,
    unregister_voicelive_orchestrator,
)
from apps.artagent.backend.voice.voicelive.settings import get_settings
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AssistantMessageItem,
    OutputTextContentPart,
    ResponseCreateParams,
    ServerEventType,
)
from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from fastapi import WebSocket
from fastapi.websockets import WebSocketState
from utils.ml_logging import get_logger

from .protocol import (
    EVENT_CONNECTED,
    EVENT_DTMF,
    EVENT_MARK,
    EVENT_MEDIA,
    EVENT_START,
    EVENT_STOP,
    TwilioProtocol,
)

if TYPE_CHECKING:
    pass

logger = get_logger("twilio.handler")

_INTERNAL_TOOL_SPEECH_PREFIXES = (
    "assistant to=functions",
    "to=functions",
    "recipient=functions",
    "<|recipient|>functions",
)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("[Twilio] Invalid %s=%r; using %.1f", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("[Twilio] Invalid %s=%r; using %d", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_CONVERSATION_IDLE_TIMEOUT_S = _env_float("TWILIO_CONVERSATION_IDLE_TIMEOUT_S", 45.0)
_CONVERSATION_IDLE_CHECK_INTERVAL_S = _env_float("TWILIO_CONVERSATION_IDLE_CHECK_INTERVAL_S", 5.0)
_INTERNAL_PLAYOUT_GENERATION_KEY = "_wulo_playout_generation"
_INTERNAL_CALLER_LINEAGE_KEY = "_wulo_caller_turn_lineage"
_NON_INTERRUPTIBLE_CALL_END_REASONS = {"urgent", "safeguarding", "distress"}
# Caller-turn events that must not reach the orchestrator while a hard-stop
# close is pending: the ignored barge-in's transcription otherwise re-runs the
# safety flow and lets the model author a reopen response (live probe
# CAea0731, 2026-07-10).
_CALLER_TURN_EVENT_TYPES = {
    ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED,
    ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA,
    ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
    ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED,
}


@dataclass
class _PendingBargeDecision:
    item_id: str | None
    transcript: str = ""
    deadline_task: asyncio.Task[None] | None = None


class _TwilioMessenger:
    """Minimal messenger interface for LiveOrchestrator in Twilio context."""

    def __init__(self, session_id: str, call_id: str | None = None) -> None:
        self._session_id = session_id
        self._call_id = call_id
        self._active_agent_name: str | None = None
        self._active_agent_label: str | None = None
        self._active_turn_id: str | None = None
        self._end_call_cb = None
        self._deterministic_speech_cb = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def call_id(self) -> str | None:
        return self._call_id

    def set_active_agent(self, agent_name: str | None) -> None:
        self._active_agent_name = agent_name
        self._active_agent_label = agent_name

    def advance_turn_for_tool(self) -> str | None:
        return self._active_turn_id

    def reset_turn_sequence(self) -> None:
        pass

    def begin_user_turn(self, turn_id: str | None) -> str | None:
        self._active_turn_id = turn_id
        return turn_id

    def resolve_user_turn_id(self, candidate: str | None) -> str | None:
        return candidate or self._active_turn_id

    def finish_user_turn(self, turn_id: str | None) -> None:
        if turn_id and self._active_turn_id == turn_id:
            self._active_turn_id = None

    async def send_user_message(self, text: str, *, turn_id: str | None = None) -> None:
        logger.info(
            "Twilio user message relayed | characters=%d",
            len(text),
        )

    async def send_assistant_message(
        self,
        text: str,
        *,
        sender: str | None = None,
        response_id: str | None = None,
        status: str | None = None,
    ) -> None:
        logger.info(
            "Twilio assistant message relayed | characters=%d",
            len(text),
        )

    async def send_assistant_streaming(
        self,
        text: str,
        *,
        sender: str | None = None,
        response_id: str | None = None,
    ) -> None:
        pass

    async def send_assistant_cancelled(
        self,
        *,
        response_id: str | None,
        sender: str | None = None,
        reason: str | None = None,
    ) -> None:
        pass

    async def send_session_update(
        self,
        *,
        agent_name: str | None,
        session_obj: Any | None,
        transport: str | None = None,
    ) -> None:
        pass

    async def send_status_update(
        self,
        text: str,
        *,
        tone: str | None = None,
        caption: str | None = None,
        sender: str | None = None,
        event_label: str = "twilio_status",
    ) -> None:
        pass

    async def notify_tool_start(
        self,
        *,
        call_id: str | None,
        name: str | None,
        args: dict[str, Any],
    ) -> None:
        logger.debug("[Twilio] Tool start: %s | session=%s", name, self._session_id)

    async def notify_tool_end(
        self,
        *,
        call_id: str | None,
        name: str | None,
        status: str,
        elapsed_ms: float,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        logger.debug("[Twilio] Tool end: %s status=%s | session=%s", name, status, self._session_id)

    async def request_call_end(self, *, reason: str | None = None) -> None:
        """Forward an orchestrator terminal-state request to the transport handler."""
        callback = self._end_call_cb
        if callback is not None:
            callback(reason or "terminal")

    async def play_deterministic_speech(
        self,
        text: str,
        *,
        speech_key: str,
        terminal_reason: str | None = None,
    ) -> bool:
        """Play exact governed text through a pre-generated VoiceLive message.

        With ``terminal_reason`` set, the handler also arms the call-end
        terminator so hang-up waits for VoiceLive completion and Twilio playout.
        """
        callback = self._deterministic_speech_cb
        if callback is None:
            return False
        return bool(await callback(text, speech_key=speech_key, terminal_reason=terminal_reason))


class TwilioVoiceLiveHandler:
    """Bridge Twilio Media Streams JSON events to Azure VoiceLive."""

    def __init__(self, *, websocket: WebSocket, session_id: str) -> None:
        self.websocket = websocket
        self.session_id = session_id
        self._protocol = TwilioProtocol(session_id)
        self._messenger = _TwilioMessenger(session_id)
        self._settings = None
        self._credential: AzureKeyCredential | AsyncTokenCredential | None = None
        self._connection = None
        self._connection_cm = None
        self._orchestrator: LiveOrchestrator | None = None
        self._running = False
        self._session_opened = False
        self._shutdown = asyncio.Event()
        self._event_task: asyncio.Task | None = None
        self._outbound_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._audio_accum = bytearray()
        self._audio_accum_lineage: int | None = None
        self._AUDIO_CHUNK_SIZE = 2000
        self._AUDIO_PACE_MS = 250
        self._UNKNOWN_RESPONSE_FALLBACK_SECONDS = 1.25
        self._BARGE_IN_AUDIO_SUPPRESSION_SECONDS = _env_float("TWILIO_BARGE_IN_AUDIO_SUPPRESSION_SECONDS", 4.0)
        # Raw RMS/peak energy on the inbound track cannot distinguish caller
        # speech from the phone's echo of the agent's own audio, so on PSTN it
        # cancels the agent mid-greeting (live 2026-07-08: rms 661-5140 echo
        # misfires on every call). Default OFF: server-side semantic VAD with
        # echo cancellation drives barge-in via INPUT_AUDIO_BUFFER_SPEECH_STARTED.
        self._LOCAL_BARGE_IN_ENABLED = _env_bool("TWILIO_LOCAL_BARGE_IN_ENABLED", False)
        self._LOCAL_BARGE_IN_RMS_THRESHOLD = _env_float("TWILIO_LOCAL_BARGE_IN_RMS_THRESHOLD", 550.0)
        self._LOCAL_BARGE_IN_PEAK_THRESHOLD = _env_int("TWILIO_LOCAL_BARGE_IN_PEAK_THRESHOLD", 2500)
        self._LOCAL_BARGE_IN_CONSECUTIVE_FRAMES = max(1, _env_int("TWILIO_LOCAL_BARGE_IN_CONSECUTIVE_FRAMES", 2))
        self._pacer_task: asyncio.Task | None = None
        self._active_response_ids: set[str] = set()
        self._interrupted_response_ids: set[str] = set()
        # Hold the first response audio until its transcript prefix proves it is
        # normal caller-facing speech. VoiceLive can rarely synthesize internal
        # `assistant to=functions.*` tokens; those must never reach PSTN.
        self._assistant_transcript_prefixes: dict[str, str] = {}
        self._quarantined_response_audio: dict[str, bytearray] = {}
        self._safe_assistant_response_ids: set[str] = set()
        self._blocked_assistant_response_ids: set[str] = set()
        self._MAX_QUARANTINED_AUDIO_BYTES = 16_000
        # Latency anchors (per assistant response): first VoiceLive audio delta
        # received and first outbound media chunk enqueued to Twilio.
        self._first_delta_logged_response_ids: set[str] = set()
        self._first_audio_delta_ts = 0.0
        self._first_chunk_pending = False
        self._unknown_response_fallback_until = 0.0
        self._barge_in_audio_suppression_until = 0.0
        self._barge_in_duplicate_guard_until = 0.0
        self._barge_in_audio_drop_count = 0
        self._local_barge_in_voice_frames = 0
        self._playout_generation = 0
        self._caller_turn_lineage = 0
        self._response_lineages: dict[str, int] = {}
        self._governed_request_lineage: int | None = None
        self._playout_lineages_pending: set[int] = set()
        self._is_playing = False
        self._pending_call_end = False
        self._call_end_reason = ""
        self._call_end_audio_seen = False
        self._call_end_response_done = False
        self._call_end_mark_name: str | None = None
        self._call_end_mark_seen = asyncio.Event()
        self._call_end_task: asyncio.Task | None = None
        self._call_end_generation = 0
        self._last_activity_ts = time.monotonic()
        self._idle_task: asyncio.Task | None = None
        self._idle_disconnect_in_progress = False
        self._session_started_ts = time.monotonic()
        self._max_call_seconds = 0.0
        self._max_duration_end_requested = False
        # Transcript capture is armed only from provider-confirmed ledger state.
        self._recording_enabled = False
        self._transcript_turns: list[dict[str, Any]] = []
        self._recording_setup_task: asyncio.Task | None = None
        self._recording_revocation_task: asyncio.Task | None = None
        self._recording_authority_wait_seconds = _env_float(
            "TWILIO_RECORDING_AUTHORITY_WAIT_SECONDS",
            60.0,
        )
        self._recording_authority_poll_seconds = 0.25
        self._recording_authority_sleep = asyncio.sleep
        self._CALL_END_AUDIO_START_TIMEOUT = 4.0
        self._CALL_END_MAX_WAIT = _env_float("TWILIO_CALL_END_MAX_WAIT_S", 35.0)
        self._CALL_END_RESPONSE_DONE_TIMEOUT = _env_float("TWILIO_CALL_END_RESPONSE_DONE_TIMEOUT_S", 10.0)
        self._CALL_END_QUEUE_DRAIN_TIMEOUT = _env_float("TWILIO_CALL_END_QUEUE_DRAIN_TIMEOUT_S", 2.0)
        self._CALL_END_MARK_TIMEOUT = _env_float("TWILIO_CALL_END_MARK_TIMEOUT_S", 20.0)
        self._CALL_END_REST_TIMEOUT = 5.0
        self._messenger._end_call_cb = self._mark_pending_call_end
        self._messenger._deterministic_speech_cb = self._play_deterministic_speech
        self._governed_speech_create_lock = asyncio.Lock()
        self._governed_response_done = asyncio.Event()
        self._governed_response_done.set()
        self._governed_response_pending = False
        self._governed_response_id: str | None = None
        self._governed_response_key: str | None = None
        self._governed_expected_text: str | None = None
        self._governed_response_candidates: set[str] = set()
        self._governed_interrupted_before_claim = False
        self._recent_governed_lines: deque[tuple[str, float]] = deque(maxlen=3)
        self._pending_barge_decisions: dict[str, _PendingBargeDecision] = {}
        self._unbound_barge_decision: _PendingBargeDecision | None = None
        self._resolved_barge_decisions: dict[str, str] = {}
        # Leak cap for a pending governed-echo barge decision, not a target
        # latency. Live gpt-4o-transcribe needs 2-5s end-to-end, so a
        # sub-second deadline always expires first and fail-opens every echo
        # to "caller", sending a spurious clear (staging dark-parity
        # regression, programme 1c3a2ced). Genuine callers still resolve
        # early via the >=4-word divergent transcription delta or the
        # transcription-completed event; this cap only bounds a stuck
        # decision when transcription never arrives, and the deferral only
        # engages inside a governed line's 4-15s echo window.
        self._BARGE_DECISION_DEADLINE_S = 8.0
        self._barge_decision_sleep = asyncio.sleep
        self._MAX_RESOLVED_BARGE_DECISIONS = 64
        self._GOVERNED_RESPONSE_TIMEOUT_S = _env_float(
            "TWILIO_GOVERNED_RESPONSE_TIMEOUT_S", 30.0
        )

    async def start(self) -> None:
        """Start outbound writer. VoiceLive connection is deferred to Twilio's start event."""
        self._running = True
        self._shutdown.clear()
        self._writer_task = asyncio.create_task(self._outbound_writer(), name="twilio-writer")
        logger.info("[Twilio] Handler started | session=%s", self.session_id)

    async def stop(self) -> None:
        """Shut down VoiceLive connection, orchestrator, and writer."""
        if not self._running:
            return
        self._running = False
        self._shutdown.set()
        self._governed_response_done.set()
        unregister_voicelive_orchestrator(self.session_id)
        await self._cancel_pending_barge_decisions()

        if self._recording_setup_task and not self._recording_setup_task.done():
            self._recording_setup_task.cancel()
            try:
                await self._recording_setup_task
            except asyncio.CancelledError:
                pass
        self._recording_setup_task = None
        if self._recording_revocation_task and not self._recording_revocation_task.done():
            self._recording_revocation_task.cancel()
            try:
                await self._recording_revocation_task
            except asyncio.CancelledError:
                pass
        self._recording_revocation_task = None
        await self._persist_transcript_on_stop()

        if self._orchestrator:
            try:
                self._orchestrator.cleanup()
            except Exception:
                logger.debug("Failed to cleanup Twilio orchestrator", exc_info=True)
            self._orchestrator = None

        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
            self._event_task = None

        if self._call_end_task:
            self._call_end_task.cancel()
            try:
                await self._call_end_task
            except asyncio.CancelledError:
                pass
            self._call_end_task = None

        await self._cancel_idle_monitor()

        if self._connection_cm:
            try:
                await self._connection_cm.__aexit__(None, None, None)
            except Exception:
                logger.debug("Error closing Twilio VoiceLive connection", exc_info=True)
            self._connection_cm = None
            self._connection = None

        if self._writer_task:
            await self._outbound_queue.put(None)
            try:
                await asyncio.wait_for(self._writer_task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                self._writer_task.cancel()
            self._writer_task = None
        logger.info("[Twilio] Handler stopped | session=%s", self.session_id)

    async def handle_text_message(self, raw: str) -> None:
        """Process one Twilio Media Streams JSON event."""
        if not self._running:
            return
        msg = self._protocol.parse_message(raw)
        if msg is None:
            return
        event = str(msg.get("event") or "")
        if event == EVENT_CONNECTED:
            logger.debug("[Twilio] Connected event | session=%s", self.session_id)
        elif event == EVENT_START:
            await self._handle_start(msg)
        elif event == EVENT_MEDIA:
            await self._handle_media(msg)
        elif event == EVENT_DTMF:
            digit = self._protocol.dtmf_digit(msg)
            if digit:
                await self._handle_dtmf(digit)
        elif event == EVENT_MARK:
            mark = msg.get("mark") if isinstance(msg.get("mark"), dict) else {}
            mark_name = str(mark.get("name") or "")
            if mark_name and mark_name == self._call_end_mark_name:
                logger.info(
                    "[Twilio] Call-end playout mark received | name=%s session=%s",
                    mark_name,
                    self.session_id,
                )
                self._call_end_mark_seen.set()
            logger.debug("[Twilio] Mark received | session=%s", self.session_id)
        elif event == EVENT_STOP:
            logger.info("[Twilio] Stop received | session=%s", self.session_id)
            self._shutdown.set()
        else:
            logger.debug("[Twilio] Unhandled event=%s | session=%s", event, self.session_id)

    async def _handle_start(self, msg: dict[str, Any]) -> None:
        self._protocol.process_start(msg)
        self.session_id = self._protocol.session_id
        self._messenger._session_id = self.session_id
        self._messenger._call_id = self._protocol.call_sid

        expected_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        if expected_sid and self._protocol.account_sid and self._protocol.account_sid != expected_sid:
            logger.warning("[Twilio] Start rejected because account SID did not match")
            self._shutdown.set()
            return

        self._session_opened = True
        try:
            await self._connect_voicelive()
        except Exception as exc:
            logger.exception("[Twilio] Failed to connect to VoiceLive | session=%s", self.session_id)
            self._shutdown.set()
            await self._enqueue_message(self._protocol.create_mark(f"voicelive-error:{exc.__class__.__name__}"))

    async def _handle_media(self, msg: dict[str, Any]) -> None:
        if not self._session_opened or not self._connection:
            return
        ulaw_bytes = self._protocol.media_payload(msg)
        if not ulaw_bytes:
            return
        try:
            await self._maybe_handle_local_barge_in(ulaw_bytes)
            pcm16_b64 = ulaw_8khz_to_pcm16_24khz_b64(ulaw_bytes)
            await self._connection.input_audio_buffer.append(audio=pcm16_b64)
        except Exception:
            logger.debug("Failed to forward Twilio audio to VoiceLive", exc_info=True)

    async def _handle_dtmf(self, digit: str) -> None:
        params = self._protocol.custom_parameters
        source = str(params.get("source") or "").strip()
        if digit == "9" and source in {
            "clinic_recall_inbound",
            "clinic_recall_voice_worker",
        }:
            self._recording_enabled = False
            stopped = False
            try:
                stopped = await self._withdraw_recording_consent()
            except Exception:
                logger.warning(
                    "[Twilio] Recording withdrawal failed closed | session=%s",
                    self.session_id,
                    exc_info=True,
                )
            if not stopped:
                await self._terminate_twilio_call("recording_withdrawal_unconfirmed")
            return
        if not self._connection or not self._orchestrator:
            return
        from azure.ai.voicelive.models import (
            ClientEventConversationItemCreate,
            ClientEventResponseCreate,
            InputTextContentPart,
            UserMessageItem,
        )

        dtmf_item = ClientEventConversationItemCreate(
            item=UserMessageItem(content=[InputTextContentPart(text=f"DTMF digit pressed: {digit}")])
        )
        await self._connection.send(dtmf_item)
        await self._connection.send(ClientEventResponseCreate())

    async def _withdraw_recording_consent(self) -> bool:
        persisted = bool(await asyncio.to_thread(self._persist_recording_withdrawal))
        if persisted:
            from src.clinic_recall.durable.recording_worker import run_runtime_batch

            clinic_id = str(self._protocol.custom_parameters.get("clinic_id") or "").strip()
            if clinic_id:
                await asyncio.to_thread(
                    run_runtime_batch,
                    clinic_id=clinic_id,
                    worker_id=f"recording-withdrawal-{uuid.uuid4().hex[:12]}",
                    limit=1,
                )
                return bool(await asyncio.to_thread(self._recording_stop_confirmed))
        return False

    def _recording_stop_confirmed(self) -> bool:
        from src.clinic_recall.db import clinic_scope, get_sessionmaker, tenant_select
        from src.clinic_recall.enums import CallRecordingStatus, ClinicPhoneProvider
        from src.clinic_recall.models import CallRecord

        clinic_id = str(self._protocol.custom_parameters.get("clinic_id") or "").strip()
        call_sid = str(self._protocol.call_sid or "").strip()
        if not clinic_id or not call_sid:
            return False
        with get_sessionmaker()() as session:
            with clinic_scope(session, clinic_id):
                record = session.execute(
                    tenant_select(CallRecord).where(
                        CallRecord.provider == ClinicPhoneProvider.TWILIO,
                        CallRecord.provider_call_id == call_sid,
                    )
                ).scalar_one_or_none()
                return bool(
                    record is not None
                    and record.recording_status
                    in {
                        CallRecordingStatus.COMPLETED,
                        CallRecordingStatus.STORED,
                        CallRecordingStatus.ABSENT,
                    }
                )

    def _persist_recording_withdrawal(self) -> bool:
        from datetime import UTC, datetime

        from src.clinic_recall.db import clinic_scope, get_sessionmaker, tenant_select
        from src.clinic_recall.enums import ClinicPhoneProvider, RecordingConsentState
        from src.clinic_recall.models import CallRecord
        from src.clinic_recall.recording import (
            RecordingConsentError,
            withdraw_recording_consent,
        )

        clinic_id = str(self._protocol.custom_parameters.get("clinic_id") or "").strip()
        call_sid = str(self._protocol.call_sid or "").strip()
        if not clinic_id or not call_sid:
            return False
        SessionLocal = get_sessionmaker()
        with SessionLocal.begin() as session:
            with clinic_scope(session, clinic_id):
                record = session.execute(
                    tenant_select(CallRecord).where(
                        CallRecord.provider == ClinicPhoneProvider.TWILIO,
                        CallRecord.provider_call_id == call_sid,
                    )
                ).scalar_one_or_none()
                if record is None or record.consent_state not in {
                    RecordingConsentState.GRANTED,
                    RecordingConsentState.WITHDRAWN,
                }:
                    return False
                try:
                    withdraw_recording_consent(
                        session,
                        clinic_id=clinic_id,
                        call_record_id=record.id,
                        source="dtmf",
                        now=datetime.now(UTC),
                    )
                except RecordingConsentError:
                    return False
                return True

    async def _connect_voicelive(self) -> None:
        self._settings = get_settings()
        self._credential = await get_voicelive_credential(self._settings)

        agents, _orchestrator_config, effective_start_agent, handoff_map = await self._resolve_agents()
        connection_model = self._settings.azure_voicelive_model
        start_agent_obj = agents.get(effective_start_agent) if agents else None
        if start_agent_obj is not None:
            try:
                vl_model = start_agent_obj.get_model_for_mode("voicelive")
                if vl_model and getattr(vl_model, "deployment_id", None):
                    connection_model = vl_model.deployment_id
            except Exception as model_err:
                logger.warning("[Twilio] Failed to resolve per-agent model: %s", model_err)

        self._connection_cm = connect(
            endpoint=self._settings.azure_voicelive_endpoint,
            credential=self._credential,
            model=connection_model,
            connection_options={
                "max_msg_size": self._settings.ws_max_msg_size,
                "heartbeat": self._settings.ws_heartbeat,
                "timeout": self._settings.ws_timeout,
            },
        )
        t0 = time.perf_counter()
        self._connection = await self._connection_cm.__aenter__()
        logger.info(
            "[Twilio] VoiceLive connected | connect_ms=%.1f session=%s",
            (time.perf_counter() - t0) * 1000,
            self.session_id,
        )

        redis_mgr = getattr(self.websocket.app.state, "redis", None) if self.websocket else None
        memo_manager = None
        if redis_mgr:
            from src.stateful.state_managment import MemoManager

            memo_manager = MemoManager.from_redis(self.session_id, redis_mgr)
            for key, value in self._protocol.custom_parameters.items():
                memo_manager.set_corememory(key, value)

        self._orchestrator = LiveOrchestrator(
            conn=self._connection,
            agents=agents,
            handoff_map=handoff_map,
            start_agent=effective_start_agent,
            audio_processor=None,
            messenger=self._messenger,
            call_connection_id=self._protocol.stream_sid or self._protocol.call_sid or self.session_id,
            transport="twilio",
            model_name=connection_model,
            memo_manager=memo_manager,
        )
        register_voicelive_orchestrator(self.session_id, self._orchestrator)
        await self._orchestrator.start(system_vars=dict(self._protocol.custom_parameters))
        self._start_idle_monitor()
        self._setup_consented_recording()
        self._event_task = asyncio.create_task(self._event_loop(), name="twilio-voicelive-events")

    async def _resolve_agents(self) -> tuple[dict, Any, str, dict[str, str]]:
        app_state = getattr(self.websocket, "app", None)
        if app_state:
            app_state = getattr(app_state, "state", None)
        if app_state and hasattr(app_state, "unified_agents") and app_state.unified_agents:
            agents = app_state.unified_agents
        else:
            agents = discover_agents()

        scenario_name = str(self._protocol.custom_parameters.get("scenario") or "rebooking").strip()
        orchestrator_config = resolve_orchestrator_config(
            session_id=self.session_id,
            scenario_name=scenario_name,
        )
        if orchestrator_config and orchestrator_config.has_scenario and orchestrator_config.agents:
            merged = dict(agents)
            merged.update(orchestrator_config.agents)
            agents = merged

        session_agent = get_session_agent(self.session_id)
        if session_agent:
            agents = dict(agents)
            agents[session_agent.name] = session_agent

        effective_start_agent = DEFAULT_START_AGENT
        if session_agent:
            effective_start_agent = session_agent.name
        elif orchestrator_config and orchestrator_config.start_agent:
            effective_start_agent = orchestrator_config.start_agent

        if orchestrator_config and orchestrator_config.handoff_map:
            handoff_map = orchestrator_config.handoff_map
        elif app_state and hasattr(app_state, "handoff_map") and app_state.handoff_map:
            handoff_map = app_state.handoff_map
        else:
            handoff_map = build_handoff_map(agents)

        logger.info(
            "[Twilio] Agents resolved | count=%d start=%s scenario=%s provider=%s inbound_call=%s session=%s",
            len(agents),
            effective_start_agent,
            scenario_name,
            self._protocol.custom_parameters.get("provider"),
            self._protocol.custom_parameters.get("inbound_call_id"),
            self.session_id,
        )
        return agents, orchestrator_config, effective_start_agent, handoff_map

    async def _event_loop(self) -> None:
        assert self._connection is not None
        try:
            async for event in self._connection:
                if self._shutdown.is_set():
                    break
                etype = event.type if hasattr(event, "type") else None
                await self._handle_voicelive_event(event, etype)
                if self._orchestrator:
                    if not self._should_forward_event_to_orchestrator(etype, event):
                        continue
                    response_id = self._extract_response_id(event)
                    if (
                        response_id in self._blocked_assistant_response_ids
                        and etype
                        in {
                            ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA,
                            ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE,
                        }
                    ):
                        continue
                    await self._orchestrator.handle_event(event)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[Twilio] VoiceLive event loop error")
        finally:
            self._shutdown.set()

    def _should_forward_event_to_orchestrator(
        self,
        etype: Any,
        event: Any | None = None,
    ) -> bool:
        """Drop caller-turn events while a hard-stop close is pending.

        The transport barge-in guard already ignores caller speech during a
        hard-stop, but the speech still transcribes; forwarding that turn to
        the orchestrator re-ran escalation writes and produced a model-authored
        reopen response during the urgent close (live probe CAea0731,
        2026-07-10). Response lifecycle events keep flowing so the governed
        close-out can complete.
        """
        if (
            self._pending_call_end
            and self._call_end_reason in _NON_INTERRUPTIBLE_CALL_END_REASONS
            and etype in _CALLER_TURN_EVENT_TYPES
        ):
            logger.info(
                "[Twilio] Caller turn not forwarded during hard-stop close | reason=%s session=%s",
                self._call_end_reason,
                self.session_id,
            )
            return False
        item_id = self._event_item_id(event)
        if etype == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA:
            return False
        if (
            item_id
            and self._resolved_barge_decisions.get(item_id) == "echo"
            and etype
            in {
                ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
                ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED,
            }
        ):
            return False
        return True

    async def _handle_voicelive_event(self, event: Any, etype: Any) -> None:
        if etype == ServerEventType.RESPONSE_CREATED:
            response_id = self._extract_response_id(event)
            if response_id:
                self._active_response_ids.add(response_id)
                self._response_lineages.setdefault(
                    response_id,
                    self._governed_request_lineage
                    if self._governed_response_pending
                    and self._governed_request_lineage is not None
                    else self._caller_turn_lineage,
                )
                await self._invalidate_stale_playout_for_lineage(
                    self._response_lineages[response_id]
                )
                self._register_governed_response_candidate(response_id)
                if (
                    self._pending_call_end
                    and self._call_end_reason in _NON_INTERRUPTIBLE_CALL_END_REASONS
                    and response_id != self._governed_response_id
                    and response_id not in self._governed_response_candidates
                ):
                    # A model/auto response created during a hard-stop close
                    # must never play (live probe CAea0731, 2026-07-10: a
                    # model-authored reopen played during the urgent close).
                    self._interrupted_response_ids.add(response_id)
                    logger.info(
                        "[Twilio] Non-governed response suppressed during hard-stop close | response=%s session=%s",
                        response_id,
                        self.session_id,
                    )
        elif etype == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
            await self._handle_assistant_transcript_prefix(
                self._extract_response_id(event),
                getattr(event, "delta", None) or getattr(event, "transcript", None),
            )
        elif etype == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            response_id = self._extract_response_id(event)
            transcript = getattr(event, "transcript", None)
            self._claim_governed_response(response_id, transcript)
            await self._handle_assistant_transcript_prefix(
                response_id,
                transcript,
            )
        elif etype == ServerEventType.RESPONSE_AUDIO_DELTA:
            delta = getattr(event, "delta", None)
            if not delta:
                return
            response_id = getattr(event, "response_id", None)
            if response_id in self._blocked_assistant_response_ids:
                self._log_suppressed_audio(source="internal_tool_speech", response_id=response_id)
                return
            if self._should_drop_interrupted_audio(response_id, now=time.perf_counter()):
                self._log_suppressed_audio(source="delta", response_id=response_id)
                return
            self._touch_activity()
            self._is_playing = True
            if response_id:
                self._active_response_ids.add(response_id)
            if response_id and response_id not in self._first_delta_logged_response_ids:
                self._first_delta_logged_response_ids.add(response_id)
                self._first_audio_delta_ts = time.perf_counter()
                self._first_chunk_pending = True
                # T3-audio latency anchor: VoiceLive started streaming audio.
                logger.info(
                    "[Twilio] First audio delta | response=%s session=%s",
                    response_id,
                    self.session_id,
                )
            if self._pending_call_end:
                self._call_end_audio_seen = True
            try:
                audio = convert_voicelive_delta_to_ulaw(delta)
                if response_id and response_id not in self._safe_assistant_response_ids:
                    quarantined = self._quarantined_response_audio.setdefault(
                        response_id,
                        bytearray(),
                    )
                    quarantined.extend(audio)
                    if len(quarantined) > self._MAX_QUARANTINED_AUDIO_BYTES:
                        await self._block_internal_tool_speech(
                            response_id,
                            reason="assistant transcript unavailable before quarantine limit",
                        )
                    return
                self._set_audio_accumulator_lineage(
                    self._response_lineages.get(
                        response_id,
                        self._caller_turn_lineage,
                    )
                )
                await self._enqueue_audio(audio)
            except Exception:
                logger.exception("Failed to convert Twilio outbound audio delta")
        elif etype in {ServerEventType.RESPONSE_AUDIO_DONE, ServerEventType.RESPONSE_DONE}:
            response_id = self._extract_response_id(event)
            if etype == ServerEventType.RESPONSE_DONE:
                self._claim_governed_response(
                    response_id,
                    self._assistant_transcript_from_response(event),
                )
                self._claim_interrupted_governed_response(response_id)
                self._complete_governed_response(response_id)
            if etype == ServerEventType.RESPONSE_DONE and response_id:
                if (
                    response_id not in self._safe_assistant_response_ids
                    and response_id not in self._blocked_assistant_response_ids
                ):
                    final_transcript = self._assistant_transcript_from_response(event)
                    if final_transcript:
                        await self._handle_assistant_transcript_prefix(
                            response_id,
                            final_transcript,
                        )
                    elif self._quarantined_response_audio.get(response_id):
                        logger.error(
                            "[Twilio] Dropped assistant audio without transcript | response=%s session=%s",
                            response_id,
                            self.session_id,
                        )
                self._assistant_transcript_prefixes.pop(response_id, None)
                self._quarantined_response_audio.pop(response_id, None)
                self._safe_assistant_response_ids.discard(response_id)
            if response_id in self._blocked_assistant_response_ids:
                if etype == ServerEventType.RESPONSE_DONE:
                    self._blocked_assistant_response_ids.discard(response_id)
                    self._interrupted_response_ids.discard(response_id)
                    if self._pending_call_end:
                        self._call_end_response_done = True
                self._clear_audio_accumulator()
                self._is_playing = False
                logger.debug(
                    "[Twilio] Skipped blocked internal-speech completion | session=%s response=%s",
                    self.session_id,
                    response_id,
                )
                return
            if self._complete_interrupted_response(response_id, now=time.perf_counter()):
                if etype == ServerEventType.RESPONSE_DONE and self._pending_call_end:
                    self._call_end_response_done = True
                self._clear_audio_accumulator()
                self._is_playing = False
                logger.debug(
                    "[Twilio] Skipped stale audio flush after barge-in | session=%s response=%s",
                    self.session_id,
                    response_id,
                )
                return
            self._touch_activity()
            # Normal response completion must not synchronously drain paced
            # audio. A multi-second drain blocks this VoiceLive event loop, so
            # speech_started cannot reach Twilio clear/cancel until the agent
            # has finished talking. The existing pacer owns normal playout;
            # only the independent call-end task uses _flush_audio_buffer().
            self._ensure_audio_pacer()
            self._is_playing = bool(
                self._audio_accum
                or (self._pacer_task is not None and not self._pacer_task.done())
            )
            if response_id:
                self._active_response_ids.discard(response_id)
            if etype == ServerEventType.RESPONSE_DONE:
                self._capture_assistant_transcript_from_response(event)
            if self._pending_call_end:
                self._call_end_response_done = True
        elif etype == ServerEventType.ERROR:
            self._fail_pending_governed_response(event)
        elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            self._touch_activity()
            if self._should_defer_governed_barge_in():
                item_id = self._event_item_id(event)
                if item_id:
                    self._begin_pending_barge_decision(item_id)
                else:
                    self._begin_unbound_barge_decision()
                logger.info(
                    "[Twilio] Caller speech awaiting governed echo check | session=%s",
                    self.session_id,
                )
            else:
                item_id = self._event_item_id(event)
                if item_id:
                    await self._confirm_caller_item(item_id)
                else:
                    await self._handle_barge_in()
        elif etype == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA:
            await self._handle_barge_transcription_delta(event)
        elif etype in {
            ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED,
            ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED,
        }:
            if await self._finish_pending_barge_decision(event, etype):
                return
            if etype == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
                transcript = getattr(event, "transcript", None)
                self._capture_transcript_turn("user", transcript)
            self._release_barge_in_suppression_after_user_turn()

    @staticmethod
    def _event_item_id(event: Any | None) -> str | None:
        item_id = str(getattr(event, "item_id", None) or "").strip()
        return item_id or None

    def _begin_pending_barge_decision(self, item_id: str) -> None:
        if item_id in self._pending_barge_decisions or item_id in self._resolved_barge_decisions:
            return
        decision = _PendingBargeDecision(item_id=item_id)
        self._pending_barge_decisions[item_id] = decision
        decision.deadline_task = asyncio.create_task(
            self._barge_decision_deadline(decision),
            name="twilio-barge-decision",
        )

    def _begin_unbound_barge_decision(self) -> None:
        if self._unbound_barge_decision is not None:
            return
        decision = _PendingBargeDecision(item_id=None)
        self._unbound_barge_decision = decision
        decision.deadline_task = asyncio.create_task(
            self._barge_decision_deadline(decision),
            name="twilio-barge-decision",
        )

    async def _barge_decision_deadline(self, decision: _PendingBargeDecision) -> None:
        try:
            await self._barge_decision_sleep(self._BARGE_DECISION_DEADLINE_S)
        except asyncio.CancelledError:
            return
        if decision.item_id is None:
            if self._unbound_barge_decision is not decision:
                return
            self._unbound_barge_decision = None
            decision.deadline_task = None
            await self._handle_barge_in()
            return
        if self._pending_barge_decisions.get(decision.item_id) is not decision:
            return
        decision.deadline_task = None
        await self._resolve_pending_barge_decision(decision.item_id, outcome="caller")

    async def _handle_barge_transcription_delta(self, event: Any) -> None:
        item_id = self._event_item_id(event)
        if not item_id or item_id in self._resolved_barge_decisions:
            return
        decision = self._pending_barge_decisions.get(item_id)
        if decision is None:
            decision = self._bind_unbound_barge_decision(item_id)
        if decision is None:
            return
        decision.transcript += str(getattr(event, "delta", None) or "")
        if len(re.findall(r"[a-z0-9]+", decision.transcript.lower())) < 4:
            return
        outcome = "echo" if self._is_recent_governed_echo(decision.transcript) else "caller"
        await self._resolve_pending_barge_decision(item_id, outcome=outcome)

    async def _finish_pending_barge_decision(self, event: Any, etype: Any) -> bool:
        item_id = self._event_item_id(event)
        if not item_id:
            decision = self._unbound_barge_decision
            if decision is None:
                return False
            if etype == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
                decision.transcript = str(getattr(event, "transcript", None) or "")
            await self._resolve_unbound_barge_decision(decision, outcome="caller")
            return False
        resolved = self._resolved_barge_decisions.get(item_id)
        if resolved is not None:
            return resolved == "echo"
        decision = self._pending_barge_decisions.get(item_id)
        if decision is None:
            decision = self._bind_unbound_barge_decision(item_id)
        if decision is None:
            await self._confirm_caller_item(item_id)
            return False
        if etype == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            transcript = str(getattr(event, "transcript", None) or "")
            if transcript:
                decision.transcript = transcript
        outcome = "echo" if self._is_recent_governed_echo(decision.transcript) else "caller"
        await self._resolve_pending_barge_decision(item_id, outcome=outcome)
        return outcome == "echo"

    def _bind_unbound_barge_decision(
        self,
        item_id: str,
    ) -> _PendingBargeDecision | None:
        if (
            self._unbound_barge_decision is None
            or self._pending_barge_decisions
            or item_id in self._resolved_barge_decisions
        ):
            return None
        decision = self._unbound_barge_decision
        self._unbound_barge_decision = None
        decision.item_id = item_id
        self._pending_barge_decisions[item_id] = decision
        return decision

    async def _resolve_pending_barge_decision(self, item_id: str, *, outcome: str) -> None:
        decision = self._pending_barge_decisions.pop(item_id, None)
        if decision is None:
            return
        deadline_task = decision.deadline_task
        if (
            deadline_task is not None
            and deadline_task is not asyncio.current_task()
            and not deadline_task.done()
        ):
            deadline_task.cancel()
            try:
                await deadline_task
            except asyncio.CancelledError:
                pass
        self._remember_barge_decision(item_id, outcome)
        if outcome == "echo":
            logger.info(
                "[Twilio] Governed speech echo suppressed | session=%s",
                self.session_id,
            )
            return
        await self._handle_barge_in(item_scoped=True)

    async def _confirm_caller_item(self, item_id: str) -> None:
        if item_id in self._resolved_barge_decisions:
            return
        self._remember_barge_decision(item_id, "caller")
        await self._handle_barge_in(item_scoped=True)

    def _remember_barge_decision(self, item_id: str, outcome: str) -> None:
        if item_id in self._resolved_barge_decisions:
            return
        if len(self._resolved_barge_decisions) >= self._MAX_RESOLVED_BARGE_DECISIONS:
            oldest_item_id = next(iter(self._resolved_barge_decisions))
            self._resolved_barge_decisions.pop(oldest_item_id, None)
        self._resolved_barge_decisions[item_id] = outcome

    async def _resolve_unbound_barge_decision(
        self,
        decision: _PendingBargeDecision,
        *,
        outcome: str,
    ) -> None:
        if self._unbound_barge_decision is not decision:
            return
        self._unbound_barge_decision = None
        deadline_task = decision.deadline_task
        if (
            deadline_task is not None
            and deadline_task is not asyncio.current_task()
            and not deadline_task.done()
        ):
            deadline_task.cancel()
            try:
                await deadline_task
            except asyncio.CancelledError:
                pass
        if outcome == "echo":
            logger.info(
                "[Twilio] Governed speech echo suppressed | session=%s",
                self.session_id,
            )
            return
        await self._handle_barge_in()

    async def _cancel_pending_barge_decisions(self) -> None:
        tasks = [
            decision.deadline_task
            for decision in self._pending_barge_decisions.values()
            if decision.deadline_task is not None and not decision.deadline_task.done()
        ]
        if (
            self._unbound_barge_decision is not None
            and self._unbound_barge_decision.deadline_task is not None
            and not self._unbound_barge_decision.deadline_task.done()
        ):
            tasks.append(self._unbound_barge_decision.deadline_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending_barge_decisions.clear()
        self._unbound_barge_decision = None

    async def _handle_assistant_transcript_prefix(
        self,
        response_id: str | None,
        delta: Any,
    ) -> None:
        if not response_id or response_id in self._blocked_assistant_response_ids:
            return
        text = str(delta or "")
        if not text:
            return
        prefix = (self._assistant_transcript_prefixes.get(response_id, "") + text)[:160]
        self._assistant_transcript_prefixes[response_id] = prefix
        self._claim_governed_response_prefix(response_id, prefix)
        normalized = " ".join(prefix.strip().lower().split())
        if any(marker in normalized for marker in _INTERNAL_TOOL_SPEECH_PREFIXES):
            await self._block_internal_tool_speech(
                response_id,
                reason="internal function-call syntax in assistant transcript",
            )
            return
        if any(marker.startswith(normalized) for marker in _INTERNAL_TOOL_SPEECH_PREFIXES):
            return

        self._safe_assistant_response_ids.add(response_id)
        quarantined = self._quarantined_response_audio.pop(response_id, None)
        if quarantined:
            self._set_audio_accumulator_lineage(
                self._response_lineages.get(response_id, self._caller_turn_lineage)
            )
            await self._enqueue_audio(bytes(quarantined))

    async def _block_internal_tool_speech(self, response_id: str, *, reason: str) -> None:
        if response_id in self._blocked_assistant_response_ids:
            return
        self._blocked_assistant_response_ids.add(response_id)
        self._interrupted_response_ids.add(response_id)
        self._assistant_transcript_prefixes.pop(response_id, None)
        self._quarantined_response_audio.pop(response_id, None)
        self._safe_assistant_response_ids.discard(response_id)
        self._playout_generation += 1
        if self._pacer_task and not self._pacer_task.done():
            self._pacer_task.cancel()
            try:
                await self._pacer_task
            except asyncio.CancelledError:
                pass
        self._pacer_task = None
        self._clear_audio_accumulator()
        self._active_response_ids.discard(response_id)
        self._is_playing = False
        preserved_items = self._drain_outbound_audio()
        clear_mode = await self._send_clear_for_barge_in()
        for item in preserved_items:
            self._outbound_queue.put_nowait(item)
        logger.error(
            "[Twilio] Blocked internal tool-call speech | response=%s reason=%s clear=%s session=%s",
            response_id,
            reason,
            clear_mode,
            self.session_id,
        )
        response = getattr(self._connection, "response", None)
        cancel = getattr(response, "cancel", None)
        if cancel is not None:
            try:
                await cancel()
            except Exception:
                logger.warning(
                    "[Twilio] Failed to cancel internal tool-call speech response | response=%s session=%s",
                    response_id,
                    self.session_id,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Consented recording + transcript persistence (deterministic gates)
    # ------------------------------------------------------------------

    def _setup_consented_recording(self) -> None:
        """Check provider-confirmed ledger authority without starting recording."""
        params = self._protocol.custom_parameters
        source = str(params.get("source") or "").strip()
        clinic_id = str(params.get("clinic_id") or "").strip()
        if (
            source not in {"clinic_recall_inbound", "clinic_recall_voice_worker"}
            or not clinic_id
            or not self._protocol.call_sid
        ):
            return
        self._recording_setup_task = asyncio.create_task(
            self._load_provider_confirmed_recording(),
            name="twilio-recording-authority",
        )

    async def _load_provider_confirmed_recording(self) -> None:
        deadline = time.monotonic() + max(self._recording_authority_wait_seconds, 0.0)
        try:
            while True:
                state = await asyncio.to_thread(self._recording_authority_state)
                if state == "confirmed":
                    self._recording_enabled = True
                    if self._running:
                        self._recording_revocation_task = asyncio.create_task(
                            self._monitor_recording_revocation(),
                            name="twilio-recording-revocation",
                        )
                    return
                if state == "closed":
                    self._recording_enabled = False
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._recording_enabled = False
                    return
                await self._recording_authority_sleep(
                    min(self._recording_authority_poll_seconds, remaining)
                )
        except Exception:
            logger.warning(
                "[Twilio] Recording authority lookup failed | session=%s",
                self.session_id,
                exc_info=True,
            )
            self._recording_enabled = False

    async def _monitor_recording_revocation(self) -> None:
        while self._running and self._recording_enabled:
            await self._recording_authority_sleep(
                self._recording_authority_poll_seconds
            )
            try:
                state = await asyncio.to_thread(self._recording_authority_state)
            except Exception:
                logger.warning(
                    "[Twilio] Recording revocation lookup failed closed | session=%s",
                    self.session_id,
                )
                self._recording_enabled = False
                return
            if state != "confirmed":
                self._recording_enabled = False
                return

    def _recording_authority_state(self) -> str:
        from src.clinic_recall.db import clinic_scope, get_sessionmaker, tenant_select
        from src.clinic_recall.enums import (
            CallRecordingStatus,
            ClinicPhoneProvider,
            RecordingConsentState,
        )
        from src.clinic_recall.models import CallRecord

        params = self._protocol.custom_parameters
        clinic_id = str(params.get("clinic_id") or "").strip()
        call_sid = str(self._protocol.call_sid or "").strip()
        if not clinic_id or not call_sid:
            return "closed"
        SessionLocal = get_sessionmaker()
        with SessionLocal() as session:
            with clinic_scope(session, clinic_id):
                record = session.execute(
                    tenant_select(CallRecord).where(
                        CallRecord.provider == ClinicPhoneProvider.TWILIO,
                        CallRecord.provider_call_id == call_sid,
                    )
                ).scalar_one_or_none()
                if record is None or record.consent_state != RecordingConsentState.GRANTED:
                    return "closed"
                if record.recording_status == CallRecordingStatus.IN_PROGRESS:
                    return "confirmed"
                if record.recording_status in {
                    CallRecordingStatus.START_PENDING,
                    CallRecordingStatus.STARTING,
                    CallRecordingStatus.RECONCILE_REQUIRED,
                }:
                    return "pending"
                return "closed"

    def _capture_transcript_turn(self, role: str, text: Any) -> None:
        """Collect a minimized turn for consented calls only."""
        if not self._recording_enabled:
            return
        cleaned = str(text or "").strip()
        if not cleaned:
            return
        self._transcript_turns.append(
            {
                "role": role,
                "text": cleaned,
                "t": round(time.monotonic() - self._session_started_ts, 1),
            }
        )

    def _capture_assistant_transcript_from_response(self, event: Any) -> None:
        """Pull the assistant transcript out of a RESPONSE_DONE event."""
        if not self._recording_enabled:
            return
        transcript = self._assistant_transcript_from_response(event)
        if transcript:
            self._capture_transcript_turn("assistant", transcript)

    @staticmethod
    def _assistant_transcript_from_response(event: Any) -> str:
        transcripts: list[str] = []
        response = getattr(event, "response", None)
        for item in getattr(response, "output", None) or []:
            for part in getattr(item, "content", None) or []:
                transcript = getattr(part, "transcript", None)
                if transcript:
                    transcripts.append(str(transcript))
        return " ".join(transcripts).strip()

    async def _persist_transcript_on_stop(self) -> None:
        """Finalize every trusted call and attach only authorized recorded turns."""
        clinic_id = str(self._protocol.custom_parameters.get("clinic_id") or "").strip()
        if not clinic_id or not self._protocol.call_sid:
            return
        try:
            await asyncio.to_thread(self._finalize_transcript_row)
            if self._recording_enabled and self._transcript_turns:
                logger.info(
                    "[Twilio] Transcript persisted | turns=%d session=%s",
                    len(self._transcript_turns),
                    self.session_id,
                )
        except Exception:
            logger.warning(
                "[Twilio] Transcript persistence failed | session=%s", self.session_id, exc_info=True
            )

    def _finalize_transcript_row(self) -> None:
        from datetime import UTC, datetime

        from src.clinic_recall.db import get_sessionmaker
        from src.clinic_recall.enums import ClinicPhoneProvider
        from src.clinic_recall.recording import finalize_call_transcript

        SessionLocal = get_sessionmaker()
        with SessionLocal.begin() as session:
            finalize_call_transcript(
                session,
                clinic_id=str(self._protocol.custom_parameters.get("clinic_id") or ""),
                provider=ClinicPhoneProvider.TWILIO,
                provider_call_id=str(self._protocol.call_sid),
                transcript=(
                    list(self._transcript_turns)
                    if self._recording_enabled and self._transcript_turns
                    else None
                ),
                ended_at=datetime.now(UTC),
            )

    def _touch_activity(self) -> None:
        """Record recent conversational activity for idle timeout tracking."""
        self._last_activity_ts = time.monotonic()

    def _start_idle_monitor(self) -> None:
        """Start a conversational inactivity monitor for Twilio calls."""
        self._max_call_seconds = self._resolve_max_call_seconds()
        self._session_started_ts = time.monotonic()
        if _CONVERSATION_IDLE_TIMEOUT_S <= 0 and self._max_call_seconds <= 0:
            logger.debug("[Twilio] Conversational idle timeout disabled | session=%s", self.session_id)
            return
        if self._idle_task and not self._idle_task.done():
            return
        self._last_activity_ts = time.monotonic()
        self._idle_disconnect_in_progress = False
        self._idle_task = asyncio.create_task(self._monitor_inactivity(), name="twilio-idle-monitor")

    def _resolve_max_call_seconds(self) -> float:
        """Deterministic max-call-duration from trusted stream parameters (e.g. demo calls)."""
        raw = self._protocol.custom_parameters.get("max_call_seconds")
        if raw in (None, ""):
            return 0.0
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning("[Twilio] Invalid max_call_seconds=%r; ignoring | session=%s", raw, self.session_id)
            return 0.0
        return max(0.0, value)

    async def _cancel_idle_monitor(self) -> None:
        """Stop the conversational inactivity monitor."""
        task = self._idle_task
        if not task or task.done():
            self._idle_task = None
            return
        task.cancel()
        if task is asyncio.current_task():
            self._idle_task = None
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._idle_task = None

    async def _monitor_inactivity(self) -> None:
        """Terminate a connected Twilio call after inactivity or max duration."""
        try:
            while self._running and not self._shutdown.is_set():
                await asyncio.sleep(_CONVERSATION_IDLE_CHECK_INTERVAL_S)
                if not self._running or self._shutdown.is_set():
                    break
                if self._check_max_duration_reached():
                    break
                if _CONVERSATION_IDLE_TIMEOUT_S <= 0:
                    continue
                idle_for = time.monotonic() - self._last_activity_ts
                if idle_for < _CONVERSATION_IDLE_TIMEOUT_S:
                    continue
                if self._idle_disconnect_in_progress or self._pending_call_end or self._call_end_task:
                    break
                self._idle_disconnect_in_progress = True
                logger.info(
                    "[Twilio] Conversational idle timeout reached (%.1fs); terminating call | session=%s",
                    idle_for,
                    self.session_id,
                )
                await self._terminate_twilio_call("idle_timeout")
                break
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("[Twilio] Idle monitor error | session=%s", self.session_id, exc_info=True)

    def _check_max_duration_reached(self) -> bool:
        """Request a graceful call end once the trusted max duration elapses."""
        if self._max_call_seconds <= 0 or self._max_duration_end_requested:
            return False
        elapsed = time.monotonic() - self._session_started_ts
        if elapsed < self._max_call_seconds:
            return False
        self._max_duration_end_requested = True
        if self._pending_call_end or self._call_end_task:
            return True
        logger.info(
            "[Twilio] Max call duration reached (%.1fs >= %.1fs); ending call | session=%s",
            elapsed,
            self._max_call_seconds,
            self.session_id,
        )
        self._mark_pending_call_end("max_call_duration")
        return True

    async def _handle_barge_in(self, *, item_scoped: bool = False) -> None:
        now = time.perf_counter()
        if (
            self._pending_call_end
            and self._call_end_reason in _NON_INTERRUPTIBLE_CALL_END_REASONS
        ):
            logger.info(
                "[Twilio] Caller speech ignored during hard-stop close | reason=%s session=%s",
                self._call_end_reason,
                self.session_id,
            )
            return
        await self._cancel_interruptible_call_end_for_barge_in()
        if not item_scoped and now < self._barge_in_duplicate_guard_until:
            return
        self._barge_in_duplicate_guard_until = now + 0.75
        self._caller_turn_lineage += 1
        self._playout_generation += 1
        self._barge_in_audio_drop_count = 0
        self._interrupted_response_ids.update(self._active_response_ids)
        if self._governed_response_pending and self._governed_response_id is None:
            self._governed_interrupted_before_claim = True
        if self._interrupted_response_ids:
            self._unknown_response_fallback_until = 0.0
        else:
            self._unknown_response_fallback_until = now + self._UNKNOWN_RESPONSE_FALLBACK_SECONDS
        self._barge_in_audio_suppression_until = now + self._BARGE_IN_AUDIO_SUPPRESSION_SECONDS
        if self._pacer_task and not self._pacer_task.done():
            self._pacer_task.cancel()
            try:
                await self._pacer_task
            except asyncio.CancelledError:
                pass
        self._pacer_task = None
        self._clear_audio_accumulator()
        self._active_response_ids.clear()
        self._is_playing = False
        preserved_items = self._drain_outbound_audio()
        clear_mode = await self._send_clear_for_barge_in()
        logger.info("[Twilio] Barge-in clear sent | mode=%s session=%s", clear_mode, self.session_id)
        for item in preserved_items:
            self._outbound_queue.put_nowait(item)

        response = getattr(self._connection, "response", None)
        cancel = getattr(response, "cancel", None)
        if cancel is None:
            return
        try:
            await cancel()
        except Exception:
            logger.debug("[Twilio] VoiceLive response cancel failed during barge-in", exc_info=True)
        else:
            logger.info("[Twilio] VoiceLive response cancel requested | session=%s", self.session_id)

    async def _cancel_interruptible_call_end_for_barge_in(self) -> None:
        if (
            not self._pending_call_end
            or self._call_end_reason in _NON_INTERRUPTIBLE_CALL_END_REASONS
        ):
            return
        reason = self._call_end_reason
        self._call_end_generation += 1
        self._pending_call_end = False
        self._call_end_reason = ""
        self._call_end_audio_seen = False
        self._call_end_response_done = False
        self._call_end_mark_name = None
        self._call_end_mark_seen.set()
        task = self._call_end_task
        self._call_end_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info(
            "[Twilio] Interruptible call-end cancelled by caller speech | reason=%s session=%s",
            reason,
            self.session_id,
        )

    def _release_barge_in_suppression_after_user_turn(self) -> None:
        if self._barge_in_audio_suppression_until <= 0 and self._unknown_response_fallback_until <= 0:
            return
        self._barge_in_audio_suppression_until = 0.0
        self._unknown_response_fallback_until = 0.0
        logger.info("[Twilio] Barge-in suppression released after user transcript | session=%s", self.session_id)

    async def _maybe_handle_local_barge_in(self, ulaw_bytes: bytes) -> None:
        if not self._LOCAL_BARGE_IN_ENABLED:
            return
        if not self._is_playing and not self._active_response_ids and not self._audio_accum:
            self._local_barge_in_voice_frames = 0
            return
        if time.perf_counter() < self._barge_in_duplicate_guard_until:
            return
        pcm = ulaw_decode(ulaw_bytes).astype("int64")
        if pcm.size == 0:
            return
        rms = float(((pcm * pcm).mean()) ** 0.5)
        peak = int(abs(pcm).max())
        if rms < self._LOCAL_BARGE_IN_RMS_THRESHOLD and peak < self._LOCAL_BARGE_IN_PEAK_THRESHOLD:
            self._local_barge_in_voice_frames = 0
            return
        self._local_barge_in_voice_frames += 1
        if self._local_barge_in_voice_frames < self._LOCAL_BARGE_IN_CONSECUTIVE_FRAMES:
            return
        self._local_barge_in_voice_frames = 0
        logger.info(
            "[Twilio] Local barge-in detected from inbound audio | rms=%.1f peak=%d session=%s",
            rms,
            peak,
            self.session_id,
        )
        await self._handle_barge_in()

    def _should_drop_interrupted_audio(self, response_id: str | None, *, now: float) -> bool:
        if response_id:
            return response_id in self._interrupted_response_ids or now < self._barge_in_audio_suppression_until
        if self._interrupted_response_ids:
            return True
        if now < self._barge_in_audio_suppression_until:
            return True
        if now < self._unknown_response_fallback_until:
            return True
        self._barge_in_audio_suppression_until = 0.0
        self._unknown_response_fallback_until = 0.0
        return False

    def _should_drop_queued_media(self, item: dict[str, Any], *, now: float) -> bool:
        generation = item.get(_INTERNAL_PLAYOUT_GENERATION_KEY)
        if generation is not None and generation != self._playout_generation:
            return True
        if now < self._barge_in_audio_suppression_until:
            return True
        if now < self._unknown_response_fallback_until:
            return True
        self._unknown_response_fallback_until = 0.0
        return False

    def _complete_interrupted_response(self, response_id: str | None, *, now: float) -> bool:
        if response_id:
            if response_id not in self._interrupted_response_ids:
                if time.perf_counter() < self._barge_in_audio_suppression_until:
                    self._barge_in_audio_suppression_until = 0.0
                    self._unknown_response_fallback_until = 0.0
                    return True
                return False
            self._interrupted_response_ids.discard(response_id)
            if not self._interrupted_response_ids:
                self._barge_in_audio_suppression_until = 0.0
                self._unknown_response_fallback_until = 0.0
            return True
        if self._interrupted_response_ids:
            return True
        if now < self._barge_in_audio_suppression_until:
            self._barge_in_audio_suppression_until = 0.0
            self._unknown_response_fallback_until = 0.0
            return True
        if now < self._unknown_response_fallback_until:
            return True
        self._barge_in_audio_suppression_until = 0.0
        self._unknown_response_fallback_until = 0.0
        return False

    @staticmethod
    def _extract_response_id(event: Any) -> str | None:
        response_id = getattr(event, "response_id", None)
        if response_id:
            return response_id
        response = getattr(event, "response", None)
        if response:
            return getattr(response, "id", None)
        return None

    def _drain_outbound_audio(
        self,
        *,
        stale_before_lineage: int | None = None,
    ) -> list[dict[str, Any] | None]:
        preserved_items: list[dict[str, Any] | None] = []
        while True:
            try:
                item = self._outbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None or item.get("event") != EVENT_MEDIA:
                preserved_items.append(item)
                continue
            item_lineage = item.get(_INTERNAL_CALLER_LINEAGE_KEY)
            if (
                stale_before_lineage is not None
                and (not isinstance(item_lineage, int) or item_lineage >= stale_before_lineage)
            ):
                preserved_items.append(item)
        return preserved_items

    async def _invalidate_stale_playout_for_lineage(self, lineage: int) -> None:
        stale_response_ids = {
            response_id
            for response_id in self._active_response_ids
            if self._response_lineages.get(response_id, lineage) < lineage
        }
        has_stale_playout = any(
            pending < lineage for pending in self._playout_lineages_pending
        )
        if not stale_response_ids and not has_stale_playout:
            return
        self._interrupted_response_ids.update(stale_response_ids)
        self._active_response_ids.difference_update(stale_response_ids)
        self._playout_generation += 1
        if (
            self._audio_accum_lineage is not None
            and self._audio_accum_lineage < lineage
        ):
            if self._pacer_task and not self._pacer_task.done():
                self._pacer_task.cancel()
                try:
                    await self._pacer_task
                except asyncio.CancelledError:
                    pass
            self._pacer_task = None
            self._clear_audio_accumulator()
            self._is_playing = False
        preserved_items = self._drain_outbound_audio(
            stale_before_lineage=lineage
        )
        clear_mode = await self._send_clear_for_barge_in()
        for item in preserved_items:
            self._outbound_queue.put_nowait(item)
        logger.info(
            "[Twilio] Stale caller-lineage playout cleared | lineage=%d mode=%s session=%s",
            lineage,
            clear_mode,
            self.session_id,
        )

    async def _send_clear_for_barge_in(self) -> str:
        clear = self._protocol.create_clear()
        if self._websocket_open:
            try:
                sent = await self._send_outbound_item(clear)
            except Exception:
                logger.debug("[Twilio] Direct barge-in clear send failed; queueing fallback", exc_info=True)
            else:
                if sent:
                    self._playout_lineages_pending.clear()
                    return "direct"
        await self._enqueue_message(clear)
        self._playout_lineages_pending.clear()
        return "queued"

    async def _play_deterministic_speech(
        self,
        text: str,
        *,
        speech_key: str,
        terminal_reason: str | None = None,
    ) -> bool:
        """Create exact governed speech inside VoiceLive's AEC-aware TTS path."""
        async with self._governed_speech_create_lock:
            if not await self._wait_for_governed_response_slot():
                return False
            if not self._running or self._connection is None:
                return False
            await self._retire_active_playout_for_governed_speech()
            # Live staging call 2026-07-10: the service rejects `cancel_previous`
            # ("'response.cancel_previous' unexpected (extra fields not permitted)"),
            # killing the entire governed response. Superseding an active response
            # is handled by the explicit response.cancel() in
            # _retire_active_playout_for_governed_speech(); never send the field.
            response_params = ResponseCreateParams(
                pre_generated_assistant_message=AssistantMessageItem(
                    content=[OutputTextContentPart(text=text)]
                ),
            )
            self._governed_response_done.clear()
            self._governed_response_pending = True
            self._governed_response_id = None
            self._governed_response_key = speech_key
            self._governed_expected_text = text
            self._governed_request_lineage = self._caller_turn_lineage
            self._governed_response_candidates.clear()
            try:
                await self._connection.response.create(response=response_params)
            except Exception:
                self._reset_governed_response()
                logger.error(
                    "[Twilio] Governed VoiceLive speech unavailable | key=%s session=%s",
                    speech_key,
                    self.session_id,
                    exc_info=True,
                )
                return False
            self._touch_activity()
            logger.info(
                "[Twilio] Governed VoiceLive speech created | key=%s terminal=%s session=%s",
                speech_key,
                terminal_reason or "-",
                self.session_id,
            )
            echo_window = min(15.0, max(4.0, len(text) / 10.0 + 2.0))
            self._recent_governed_lines.append((text, time.monotonic() + echo_window))
            if terminal_reason is not None and not self._pending_call_end:
                self._mark_pending_call_end(terminal_reason)
            return True

    def _should_defer_governed_barge_in(self) -> bool:
        if self._pending_call_end:
            return False
        now = time.monotonic()
        self._recent_governed_lines = deque(
            (
                (text, expires_at)
                for text, expires_at in self._recent_governed_lines
                if expires_at > now
            ),
            maxlen=3,
        )
        return bool(self._recent_governed_lines)

    def _is_recent_governed_echo(self, transcript: Any) -> bool:
        incoming_words = re.findall(r"[a-z0-9]+", str(transcript or "").lower())
        if len(incoming_words) < 4:
            return False
        now = time.monotonic()
        for governed_text, expires_at in self._recent_governed_lines:
            if expires_at <= now:
                continue
            governed_words = re.findall(r"[a-z0-9]+", governed_text.lower())
            compared_length = min(len(incoming_words), len(governed_words))
            if compared_length < 4:
                continue
            incoming_prefix = " ".join(incoming_words[:compared_length])
            governed_prefix = " ".join(governed_words[:compared_length])
            if SequenceMatcher(None, incoming_prefix, governed_prefix).ratio() >= 0.9:
                return True
        return False

    async def _retire_active_playout_for_governed_speech(self) -> None:
        has_pacer = self._pacer_task is not None and not self._pacer_task.done()
        if not (self._active_response_ids or self._is_playing or self._audio_accum or has_pacer):
            return
        if not self._active_response_ids:
            # response.done proves VoiceLive generation, not Twilio playout.
            # Preserve ordered media and append the next governed line behind it.
            logger.info(
                "[Twilio] Prior governed playout preserved for ordered continuation | session=%s",
                self.session_id,
            )
            return
        interrupted_count = len(self._active_response_ids)
        self._playout_generation += 1
        self._interrupted_response_ids.update(self._active_response_ids)
        self._active_response_ids.clear()
        if has_pacer:
            self._pacer_task.cancel()
            try:
                await self._pacer_task
            except asyncio.CancelledError:
                pass
        self._pacer_task = None
        self._clear_audio_accumulator()
        self._is_playing = False
        preserved_items = self._drain_outbound_audio()
        clear_mode = await self._send_clear_for_barge_in()
        for item in preserved_items:
            self._outbound_queue.put_nowait(item)
        response = getattr(self._connection, "response", None)
        cancel = getattr(response, "cancel", None)
        if interrupted_count and cancel is not None:
            try:
                await cancel()
            except Exception:
                logger.debug(
                    "[Twilio] Failed to cancel prior response before governed speech",
                    exc_info=True,
                )
        logger.info(
            "[Twilio] Prior playout retired for governed speech | responses=%d clear=%s session=%s",
            interrupted_count,
            clear_mode,
            self.session_id,
        )

    async def _wait_for_governed_response_slot(self) -> bool:
        if self._governed_response_done.is_set():
            return True
        try:
            await asyncio.wait_for(
                self._governed_response_done.wait(),
                timeout=self._GOVERNED_RESPONSE_TIMEOUT_S,
            )
            return True
        except TimeoutError:
            logger.error(
                "[Twilio] Governed VoiceLive response timed out | key=%s session=%s",
                self._governed_response_key or "unknown",
                self.session_id,
            )
            response_ids = set(self._governed_response_candidates)
            if self._governed_response_id:
                response_ids.add(self._governed_response_id)
            self._interrupted_response_ids.update(response_ids)
            self._active_response_ids.difference_update(response_ids)
            self._playout_generation += 1
            if self._pacer_task and not self._pacer_task.done():
                self._pacer_task.cancel()
                try:
                    await self._pacer_task
                except asyncio.CancelledError:
                    pass
            self._pacer_task = None
            self._clear_audio_accumulator()
            self._is_playing = False
            preserved_items = self._drain_outbound_audio()
            await self._send_clear_for_barge_in()
            for item in preserved_items:
                self._outbound_queue.put_nowait(item)
            response = getattr(self._connection, "response", None)
            cancel = getattr(response, "cancel", None)
            if cancel is not None:
                try:
                    await cancel()
                except Exception:
                    logger.debug(
                        "[Twilio] Failed to cancel timed-out governed response",
                        exc_info=True,
                    )
            self._reset_governed_response()
            return False

    def _register_governed_response_candidate(self, response_id: str) -> None:
        if not self._governed_response_pending or self._governed_response_id is not None:
            return
        self._governed_response_candidates.add(response_id)
        if (
            self._pending_call_end
            and self._call_end_reason in _NON_INTERRUPTIBLE_CALL_END_REASONS
        ):
            self._interrupted_response_ids.discard(response_id)
        elif self._governed_interrupted_before_claim:
            self._interrupted_response_ids.add(response_id)

    def _claim_governed_response(
        self,
        response_id: str | None,
        transcript: Any,
    ) -> None:
        if (
            not response_id
            or response_id not in self._governed_response_candidates
            or not self._governed_response_pending
            or self._governed_response_id is not None
        ):
            return
        # VoiceLive 2026-04-10 does not echo ResponseCreateParams.metadata or a
        # client-supplied assistant item ID. Pre-generated speech does return
        # its exact source text on transcript.done before response.done, so use
        # that live-proven, content-exact signal without logging or persisting it.
        if not self._governed_expected_text or not transcript:
            return
        if transcript != self._governed_expected_text:
            if (
                response_id in self._interrupted_response_ids
                and self._governed_expected_text.startswith(str(transcript))
            ):
                self._assistant_transcript_prefixes.setdefault(response_id, str(transcript))
                return
            self._governed_response_candidates.discard(response_id)
            return
        self._governed_response_pending = False
        self._governed_response_id = response_id
        logger.info(
            "[Twilio] Governed VoiceLive response started | key=%s response=%s session=%s",
            self._governed_response_key or "unknown",
            response_id,
            self.session_id,
        )

    def _claim_governed_response_prefix(self, response_id: str, prefix: str) -> None:
        if (
            response_id not in self._governed_response_candidates
            or not self._governed_response_pending
            or not self._governed_expected_text
        ):
            return
        if not self._governed_expected_text.startswith(prefix):
            self._governed_response_candidates.discard(response_id)
            return
        if len(prefix) < min(32, len(self._governed_expected_text)):
            return
        self._governed_response_pending = False
        self._governed_response_id = response_id
        logger.info(
            "[Twilio] Governed VoiceLive response started | key=%s response=%s session=%s",
            self._governed_response_key or "unknown",
            response_id,
            self.session_id,
        )

    def _claim_interrupted_governed_response(self, response_id: str | None) -> None:
        if (
            not response_id
            or response_id not in self._governed_response_candidates
            or response_id not in self._interrupted_response_ids
            or not self._governed_response_pending
            or not self._governed_expected_text
        ):
            return
        prefix = self._assistant_transcript_prefixes.get(response_id, "")
        if not prefix or not self._governed_expected_text.startswith(prefix):
            return
        self._governed_response_pending = False
        self._governed_response_id = response_id
        logger.info(
            "[Twilio] Interrupted governed response identified | key=%s response=%s session=%s",
            self._governed_response_key or "unknown",
            response_id,
            self.session_id,
        )

    def _fail_pending_governed_response(self, event: Any) -> None:
        """Release the governed slot when VoiceLive rejects the create.

        A service-side validation error never produces a `response.created`,
        so without this the slot stalls for the full timeout and every later
        governed line queues behind dead air (live staging call 2026-07-10).
        Only fires while a governed request is awaiting its response claim.
        """
        if not self._governed_response_pending or self._governed_response_id is not None:
            return
        error = getattr(event, "error", None)
        error_type = str(getattr(error, "type", None) or "")
        error_param = str(getattr(error, "param", None) or "")
        if error_type != "invalid_request_error" or not error_param.startswith("response."):
            return
        logger.error(
            "[Twilio] Governed VoiceLive response rejected | key=%s code=%s session=%s",
            self._governed_response_key or "unknown",
            getattr(error, "code", None) or "unknown",
            self.session_id,
        )
        self._reset_governed_response()

    def _complete_governed_response(self, response_id: str | None) -> None:
        if not response_id or response_id != self._governed_response_id:
            return
        logger.info(
            "[Twilio] Governed VoiceLive response completed | key=%s response=%s session=%s",
            self._governed_response_key or "unknown",
            response_id,
            self.session_id,
        )
        self._reset_governed_response()

    def _reset_governed_response(self) -> None:
        self._governed_response_pending = False
        self._governed_response_id = None
        self._governed_response_key = None
        self._governed_expected_text = None
        self._governed_request_lineage = None
        self._governed_response_candidates.clear()
        self._governed_interrupted_before_claim = False
        self._governed_response_done.set()

    def _mark_pending_call_end(self, reason: str) -> None:
        """Record an orchestrator request to end the call and start the terminator once."""
        requested_reason = reason or "terminal"
        if self._pending_call_end and self._call_end_task and not self._call_end_task.done():
            if (
                requested_reason in _NON_INTERRUPTIBLE_CALL_END_REASONS
                and self._call_end_reason not in _NON_INTERRUPTIBLE_CALL_END_REASONS
            ):
                # A hard-stop must supersede a pending interruptible close;
                # otherwise caller speech could cancel an urgent termination.
                logger.info(
                    "[Twilio] Upgrading pending call-end to hard-stop | from=%s to=%s session=%s",
                    self._call_end_reason,
                    requested_reason,
                    self.session_id,
                )
                stale_task = self._call_end_task
                self._call_end_task = None
                self._call_end_audio_seen = False
                if stale_task is not asyncio.current_task():
                    stale_task.cancel()
            else:
                logger.info(
                    "[Twilio] Terminal call-end already pending | reason=%s session=%s",
                    self._call_end_reason,
                    self.session_id,
                )
                return
        self._call_end_generation += 1
        generation = self._call_end_generation
        self._pending_call_end = True
        self._call_end_reason = requested_reason
        self._call_end_response_done = False
        protected_ids: set[str] = set()
        hard_stop = self._call_end_reason in _NON_INTERRUPTIBLE_CALL_END_REASONS
        if hard_stop:
            protected_ids.update(self._governed_response_candidates)
            if self._governed_response_id:
                protected_ids.add(self._governed_response_id)
            self._interrupted_response_ids.difference_update(protected_ids)
            # A pre-arm barge-in must not mute or stale-mark the protected
            # close-out: its time-based suppression window and pre-claim
            # classification would otherwise drop the governed hard-stop
            # audio (live probes CAea0731/CA6dfeef, 2026-07-10). Known-stale
            # responses stay suppressed via _interrupted_response_ids.
            self._governed_interrupted_before_claim = False
            self._barge_in_audio_suppression_until = 0.0
            self._unknown_response_fallback_until = 0.0
        if self._active_response_ids:
            stale_ids = self._active_response_ids - protected_ids
            if stale_ids:
                self._interrupted_response_ids.update(stale_ids)
                if not hard_stop:
                    self._barge_in_audio_suppression_until = max(
                        self._barge_in_audio_suppression_until,
                        time.perf_counter() + self._BARGE_IN_AUDIO_SUPPRESSION_SECONDS,
                    )
                self._clear_audio_accumulator()
            self._active_response_ids = protected_ids
        logger.info(
            "[Twilio] Terminal call-end requested | reason=%s session=%s",
            self._call_end_reason,
            self.session_id,
        )
        if self._call_end_task is None:
            try:
                self._call_end_task = asyncio.create_task(
                    self._terminate_twilio_call(
                        self._call_end_reason,
                        generation=generation,
                    ),
                    name="twilio-call-end",
                )
            except RuntimeError:
                logger.debug("[Twilio] No running loop to schedule call termination", exc_info=True)

    async def _terminate_twilio_call(
        self,
        reason: str,
        *,
        generation: int | None = None,
    ) -> None:
        """Mirror ART's terminate_session: let the close-out play, hang up via REST, then close the WS."""
        if generation is not None and not self._call_end_is_current(generation):
            return
        overall_deadline = time.perf_counter() + self._CALL_END_MAX_WAIT
        # 1. Let the final close-out audio START so the caller hears the sign-off.
        audio_start_deadline = time.perf_counter() + self._CALL_END_AUDIO_START_TIMEOUT
        while (
            self._running
            and not self._call_end_audio_seen
            and time.perf_counter() < audio_start_deadline
        ):
            await asyncio.sleep(0.05)
        # 2. Let the close-out response finish producing audio when possible,
        # then flush local audio into Twilio while reserving time for the playout
        # mark. Do not spend the whole deadline before sending the mark.
        await self._wait_for_call_end_response_done(overall_deadline)
        if generation is not None and not self._call_end_is_current(generation):
            return
        await self._drain_call_end_audio_to_twilio(overall_deadline)
        if generation is not None and not self._call_end_is_current(generation):
            return
        await self._wait_for_twilio_playout_mark(overall_deadline)
        if generation is not None and not self._call_end_is_current(generation):
            logger.info(
                "[Twilio] Stale call-end aborted before REST | reason=%s session=%s",
                reason,
                self.session_id,
            )
            return
        logger.info(
            "[Twilio] Terminating call | reason=%s session=%s",
            reason,
            self.session_id,
        )
        # 3. Provider-side hangup (reliable) — mirrors ACS _hangup_acs_call.
        await self._complete_twilio_call_via_rest(reason)
        # 4. Close the websocket (fallback + cleanup).
        self._shutdown.set()
        try:
            if self._websocket_open:
                await self.websocket.close()
        except Exception:
            logger.debug("[Twilio] websocket close on terminate failed", exc_info=True)

    def _call_end_is_current(self, generation: int) -> bool:
        return (
            self._pending_call_end
            and self._call_end_generation == generation
        )

    async def _wait_for_call_end_response_done(self, overall_deadline: float) -> None:
        """Wait briefly for the final assistant response to finish producing."""
        reserve = self._CALL_END_MARK_TIMEOUT if self._websocket_open and self._protocol.stream_sid else 0.0
        reserve = min(reserve, max(0.0, overall_deadline - time.perf_counter()))
        deadline = min(time.perf_counter() + self._CALL_END_RESPONSE_DONE_TIMEOUT, overall_deadline - reserve)
        while self._running and not self._call_end_response_done and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)
        if self._pending_call_end and not self._call_end_response_done:
            logger.info("[Twilio] Call-end response still producing; draining queued audio before mark")

    async def _drain_call_end_audio_to_twilio(self, overall_deadline: float) -> None:
        """Flush close-out audio locally, then wait briefly for the writer queue."""
        try:
            await self._flush_audio_buffer()
        except Exception:
            logger.debug("[Twilio] call-end audio flush failed", exc_info=True)
        reserve = self._CALL_END_MARK_TIMEOUT if self._websocket_open and self._protocol.stream_sid else 0.0
        reserve = min(reserve, max(0.0, overall_deadline - time.perf_counter()))
        drain_deadline = min(time.perf_counter() + self._CALL_END_QUEUE_DRAIN_TIMEOUT, overall_deadline - reserve)
        while self._running and time.perf_counter() < drain_deadline:
            drained = (
                not self._audio_accum
                and self._outbound_queue.empty()
                and (self._pacer_task is None or self._pacer_task.done())
            )
            if drained:
                break
            await asyncio.sleep(0.05)

    async def _wait_for_twilio_playout_mark(self, overall_deadline: float) -> None:
        """Wait until Twilio confirms playback of all queued close-out audio."""
        if not self._websocket_open or not self._protocol.stream_sid:
            return
        remaining = overall_deadline - time.perf_counter()
        timeout = min(self._CALL_END_MARK_TIMEOUT, remaining)
        if timeout <= 0:
            return
        mark_name = f"call-end-{uuid.uuid4().hex[:8]}"
        self._call_end_mark_name = mark_name
        self._call_end_mark_seen.clear()
        await self._enqueue_message(self._protocol.create_mark(mark_name))
        logger.info(
            "[Twilio] Call-end playout mark sent | name=%s timeout=%.1fs session=%s",
            mark_name,
            timeout,
            self.session_id,
        )
        try:
            await asyncio.wait_for(self._call_end_mark_seen.wait(), timeout=timeout)
        except TimeoutError:
            logger.info(
                "[Twilio] Call-end playout mark timeout; proceeding with hangup | name=%s session=%s",
                mark_name,
                self.session_id,
            )
        finally:
            if self._call_end_mark_name == mark_name:
                self._call_end_mark_name = None

    async def _complete_twilio_call_via_rest(self, reason: str) -> bool:
        """Best-effort Twilio REST hangup (Status=completed); tolerate already-completed/not-found."""
        call_sid = self._protocol.call_sid
        account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        if not (call_sid and account_sid and auth_token):
            logger.info(
                "[Twilio] REST hangup skipped (missing sid/creds); relying on websocket close | session=%s",
                self.session_id,
            )
            return False
        base = os.getenv("TWILIO_API_BASE_URL", "https://api.twilio.com").rstrip("/")
        url = f"{base}/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
        try:
            async with httpx.AsyncClient(timeout=self._CALL_END_REST_TIMEOUT) as client:
                response = await client.post(
                    url,
                    data={"Status": "completed"},
                    auth=(account_sid, auth_token),
                )
        except Exception:
            logger.debug("[Twilio] REST hangup request failed", exc_info=True)
            return False
        if response.status_code < 400:
            logger.info("[Twilio] Call completed via REST | reason=%s session=%s", reason, self.session_id)
            return True
        body = (response.text or "").lower()
        if response.status_code in {404, 409} or "already" in body or "not found" in body:
            logger.info("[Twilio] Call already completed/not found on REST hangup | session=%s", self.session_id)
            return True
        logger.warning(
            "[Twilio] REST hangup returned %s | session=%s", response.status_code, self.session_id
        )
        return False

    async def _enqueue_message(self, msg: dict[str, Any]) -> None:
        await self._outbound_queue.put(msg)

    async def _enqueue_audio(self, data: bytes) -> None:
        if not data:
            return
        if self._audio_accum_lineage is None:
            self._audio_accum_lineage = self._caller_turn_lineage
        self._audio_accum.extend(data)
        self._ensure_audio_pacer()

    def _set_audio_accumulator_lineage(self, lineage: int) -> None:
        if (
            self._audio_accum
            and self._audio_accum_lineage is not None
            and self._audio_accum_lineage != lineage
        ):
            self._clear_audio_accumulator()
        self._audio_accum_lineage = lineage

    def _clear_audio_accumulator(self) -> None:
        self._audio_accum.clear()
        self._audio_accum_lineage = None

    def _ensure_audio_pacer(self) -> None:
        if not self._running or not self._audio_accum:
            return
        if self._pacer_task is None or self._pacer_task.done():
            self._pacer_task = asyncio.create_task(self._audio_pacer(), name="twilio-audio-pacer")

    async def _audio_pacer(self) -> None:
        # Latency: send whatever audio is already buffered IMMEDIATELY, then
        # pace subsequent chunks at realtime. The previous loop slept
        # _AUDIO_PACE_MS before the first dequeue, adding a fixed 250 ms to the
        # start of every assistant reply. Pacing is proportional to bytes sent
        # (8000 B/s μ-law realtime; 2000 B ≙ 250 ms) so a small first chunk is
        # followed quickly instead of stuttering behind a fixed sleep.
        try:
            while self._running:
                if not self._audio_accum:
                    return
                chunk_size = min(self._AUDIO_CHUNK_SIZE, len(self._audio_accum))
                chunk = bytes(self._audio_accum[:chunk_size])
                lineage = self._audio_accum_lineage
                del self._audio_accum[:chunk_size]
                self._log_first_chunk_sent()
                await self._outbound_queue.put(
                    self._create_media_message(chunk, lineage=lineage)
                )
                await asyncio.sleep(chunk_size / 8000.0)
        except asyncio.CancelledError:
            pass
        finally:
            if not self._audio_accum:
                self._is_playing = False
                self._audio_accum_lineage = None

    def _log_first_chunk_sent(self) -> None:
        """T4a latency anchor: first outbound media chunk for this response."""
        if not self._first_chunk_pending:
            return
        self._first_chunk_pending = False
        delta_ms = (
            (time.perf_counter() - self._first_audio_delta_ts) * 1000.0
            if self._first_audio_delta_ts
            else -1.0
        )
        logger.info(
            "[Twilio] First audio chunk sent | delta_to_send_ms=%.1f session=%s",
            delta_ms,
            self.session_id,
        )

    async def _flush_audio_buffer(self) -> None:
        # This path is call-end-only: queue every remaining media chunk before
        # the mark and let Twilio's mark acknowledgement govern carrier playout.
        if self._pacer_task and not self._pacer_task.done():
            self._pacer_task.cancel()
            try:
                await self._pacer_task
            except asyncio.CancelledError:
                pass
        while self._audio_accum:
            chunk_size = min(self._AUDIO_CHUNK_SIZE, len(self._audio_accum))
            chunk = bytes(self._audio_accum[:chunk_size])
            lineage = self._audio_accum_lineage
            del self._audio_accum[:chunk_size]
            self._log_first_chunk_sent()
            await self._outbound_queue.put(
                self._create_media_message(chunk, lineage=lineage)
            )
        self._audio_accum_lineage = None

    def _create_media_message(
        self,
        chunk: bytes,
        *,
        lineage: int | None = None,
    ) -> dict[str, Any]:
        msg = self._protocol.create_media(chunk)
        msg[_INTERNAL_PLAYOUT_GENERATION_KEY] = self._playout_generation
        msg[_INTERNAL_CALLER_LINEAGE_KEY] = (
            self._caller_turn_lineage if lineage is None else lineage
        )
        self._playout_lineages_pending.add(msg[_INTERNAL_CALLER_LINEAGE_KEY])
        return msg

    def _public_outbound_item(self, item: dict[str, Any]) -> dict[str, Any]:
        if (
            _INTERNAL_PLAYOUT_GENERATION_KEY not in item
            and _INTERNAL_CALLER_LINEAGE_KEY not in item
        ):
            return item
        public_item = dict(item)
        public_item.pop(_INTERNAL_PLAYOUT_GENERATION_KEY, None)
        public_item.pop(_INTERNAL_CALLER_LINEAGE_KEY, None)
        return public_item

    def _log_suppressed_audio(self, *, source: str, response_id: str | None = None) -> None:
        self._barge_in_audio_drop_count += 1
        if self._barge_in_audio_drop_count <= 5 or self._barge_in_audio_drop_count % 20 == 0:
            logger.info(
                "[Twilio] Suppressed outbound audio after barge-in | source=%s response=%s drops=%d session=%s",
                source,
                response_id,
                self._barge_in_audio_drop_count,
                self.session_id,
            )

    async def _send_outbound_item(self, item: dict[str, Any]) -> bool:
        async with self._send_lock:
            if not self._websocket_open:
                return False
            if item.get("event") == EVENT_MEDIA and self._should_drop_queued_media(item, now=time.perf_counter()):
                self._log_suppressed_audio(source="writer")
                return False
            await self.websocket.send_text(json.dumps(self._public_outbound_item(item)))
            return True

    async def _outbound_writer(self) -> None:
        try:
            while self._running or not self._outbound_queue.empty():
                try:
                    item = await asyncio.wait_for(self._outbound_queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if item is None:
                    break
                if not self._websocket_open:
                    continue
                if item.get("event") == EVENT_MEDIA and self._should_drop_queued_media(item, now=time.perf_counter()):
                    self._log_suppressed_audio(source="writer")
                    continue
                await self._send_outbound_item(item)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[Twilio] Outbound writer error")

    @property
    def _websocket_open(self) -> bool:
        try:
            return (
                self.websocket.client_state == WebSocketState.CONNECTED
                and self.websocket.application_state == WebSocketState.CONNECTED
            )
        except Exception:
            return False