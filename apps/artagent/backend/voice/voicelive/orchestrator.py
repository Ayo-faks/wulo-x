"""
VoiceLive Orchestrator
=======================

Orchestrates agent switching and tool execution for VoiceLive multi-agent system.

All tool execution flows through the shared tool registry for centralized management:
- Handoff tools → trigger agent switching
- Business tools → execute and return results to model

Architecture:
    VoiceLiveSDKHandler
           │
           ▼
    LiveOrchestrator ─► UnifiedAgent registry
           │                    │
           ├─► handle_event()   └─► apply_voicelive_session()
           │                        trigger_voicelive_response()
           └─► _execute_tool_call() ───► shared tool registry

Usage:
    from apps.artagent.backend.voice.voicelive import (
        LiveOrchestrator,
        TRANSFER_TOOL_NAMES,
        CALL_CENTER_TRIGGER_PHRASES,
    )

    orchestrator = LiveOrchestrator(
        conn=voicelive_connection,
        agents=unified_agents,  # dict[str, UnifiedAgent]
        handoff_map=handoff_map,
        start_agent="Concierge",
    )
    await orchestrator.start(system_vars={...})
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

# Self-contained tool registry (no legacy vlagent dependency)
from apps.artagent.backend.registries.toolstore import (
    execute_tool,
    initialize_tools,
)
from apps.artagent.backend.src.services.session_loader import load_user_profile_by_client_id
from apps.artagent.backend.voice.handoffs import sanitize_handoff_context
from apps.artagent.backend.voice.shared.handoff_service import HandoffService
from apps.artagent.backend.voice.shared.metrics import OrchestratorMetrics
from apps.artagent.backend.voice.shared.session_state import (
    sync_state_from_memo,
    sync_state_to_memo,
)
from azure.ai.voicelive.models import (
    AssistantMessageItem,
    FunctionCallOutputItem,
    InputTextContentPart,
    OutputTextContentPart,
    ResponseCreateParams,
    ServerEventType,
    UserMessageItem,
)
from opentelemetry import trace

if TYPE_CHECKING:
    from src.stateful.state_managment import MemoManager

from apps.artagent.backend.config.constants import STOP_WORDS
from apps.artagent.backend.registries.agentstore.base import UnifiedAgent
from apps.artagent.backend.src.orchestration.naming import find_agent_by_name
from apps.artagent.backend.src.utils.tracing import (
    create_service_dependency_attrs,
    create_service_handler_attrs,
)
from src.clinic_recall.clinic_info import (
    SUPPORTED_CLINIC_FAQ_TOPICS,
    ClinicFaqTopic,
    classify_clinic_faq_topic,
    format_sample_clinic_faq_answer,
)
from src.clinic_recall.enums import EscalationReason, InteractionIntent
from src.clinic_recall.messaging.inbound import (
    CLINICAL_TERMS,
    OPT_OUT_TERMS,
    URGENT_TERMS,
    classify_intent,
    is_conversational_acknowledgement,
)
from src.clinic_recall.telemetry import emit_runtime_event
from src.enums.monitoring import GenAIOperation, GenAIProvider, SpanAttr
from utils.ml_logging import get_logger

logger = get_logger("voicelive.orchestrator")
tracer = trace.get_tracer(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

TRANSFER_TOOL_NAMES = {"transfer_call_to_destination", "transfer_call_to_call_center"}

CALL_CENTER_TRIGGER_PHRASES = {
    "transfer to call center",
    "transfer me to the call center",
}

# Benign VoiceLive server-error codes emitted when a barge-in / response.cancel
# races a response that already finished. These are expected and must NOT be
# logged as errors or surfaced to the UI. Mirrors the handler's suppression set.
_BENIGN_ERROR_CODES = {
    "response_cancel_not_active",
    "response_cancel_no_active_response",
}

_ACTIVE_RESPONSE_CREATE_ERROR_TEXT = "already has an active response"

_GREETING_FALLBACK_DELAY_SECONDS = 2.0

_IDENTITY_PROMPT_CUES = {
    "confirm your name",
    "full name",
    "your name",
    "what's your full name",
    "what is your full name",
    "who am i speaking",
    "who am i speaking with",
    "who i'm speaking",
    "who i'm speaking with",
    "who i’m speaking",
    "who i’m speaking with",
    "can i get your name",
    "could i get your name",
    "may i have your name",
    "are you the person",
    "are you the patient",
    "am i speaking to",
    "am i speaking with",
    "speaking with",
    "the right person",
    "person we're calling",
    "person we are calling",
    "am i talking to",
}

_CONSENT_PROMPT_CUES = {
    "good time to talk",
    "is now a good time",
    "happy to continue",
    "okay to continue",
    "ok to continue",
    "alright to continue",
    "record this call",
    "recording this call",
}

# Assistant prompts that put the call into a scheduling turn, so a bare date/time
# answer is expected (not an ambiguous safety signal).
_SCHEDULING_PROMPT_CUES = {
    "what date",
    "which date",
    "what day",
    "which day",
    "what time",
    "which time",
    "what days",
    "what times",
    "days or times",
    "day or time",
    "date or time",
    "preferred time",
    "preferred day",
    "preferred date",
    "date would you like",
    "time would you like",
    "day would you like",
    "when works",
    "when would",
    "what works for you",
    "day works",
    "time works",
    "day suits",
    "time suits",
}

_SCHEDULING_ANSWER_WORDS = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "today",
    "tomorrow",
    "tonight",
    "morning",
    "afternoon",
    "evening",
    "noon",
    "midday",
    "midnight",
    "weekend",
    "anytime",
    "whenever",
    "oclock",
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
    "eleventh",
    "twelfth",
    "thirteenth",
    "fourteenth",
    "fifteenth",
    "sixteenth",
    "seventeenth",
    "eighteenth",
    "nineteenth",
    "twentieth",
    "thirtieth",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "fifteen",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "quarter",
    "half",
}

_SCHEDULING_ANSWER_PHRASES = {
    "next week",
    "this week",
    "next month",
    "the weekend",
    "any time",
    "o'clock",
}

_SCHEDULING_REQUEST_PHRASES = {
    "available appointment",
    "available appointments",
    "available days",
    "available times",
    "availability",
    "check available",
    "check for available",
    "closest availability",
    "closest available",
    "earliest appointment",
    "earliest availability",
    "find availability",
    "dates available",
    "dates do you have",
    "do you have available",
    "nearest appointment",
    "nearest date",
    "next available",
    "other days",
    "what is the closest",
    "what's the closest",
}

_SAFE_CLINIC_LOGISTICS_PHRASES = {
    "are you open",
    "close today",
    "close tomorrow",
    "closing time",
    "do you offer",
    "do you provide",
    "opening hours",
    "opening time",
    "services do you offer",
    "services do you provide",
    "what services",
    "what time do you close",
    "what time do you open",
    "what time does clinic close",
    "what time does clinic open",
    "what time does the clinic close",
    "what time does the clinic open",
    "when does the clinic close",
    "when does the clinic open",
    "when are you open",
}

_BOOKING_REQUEST_TERMS = {
    "appointment",
    "available",
    "availability",
    "book",
    "booking",
    "rebook",
    "reschedule",
    "schedule",
    "slot",
}

_ASYNC_BUSINESS_TOOL_NAMES = {
    "book_slot",
    "create_inbound_booking_request",
    "find_possible_patient_match",
    "get_availability",
    "get_available_slots",
    "get_clinic_faq",
    "get_clinic_hours",
    "get_clinic_services",
    "request_callback",
    "reschedule",
}

# Distress cues that must still fail closed even inside a scheduling turn.
_SCHEDULING_DISTRESS_TERMS = {
    "worried",
    "worry",
    "concerned",
    "anxious",
    "nervous",
    "scared",
    "afraid",
}

_SCHEDULING_TIME_PATTERN = re.compile(
    r"\b\d{1,2}\s*(?:am|pm)\b|\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}(?:st|nd|rd|th)\b|\b\d{1,2}\b"
)

_AFFIRMATIVE_IDENTITY_ANSWERS = {
    "speaking",
    "yes speaking",
    "that's me",
    "thats me",
    "this is me",
    "this is he",
    "this is she",
    "yes it's me",
    "yes its me",
}

_CLOSING_ACK_PHRASES = {
    "thank you",
    "thanks",
    "thank you very much",
    "thanks very much",
    "thank you so much",
    "ok thank you",
    "okay thank you",
    "alright thank you",
    "thank you bye",
    "thanks bye",
    "bye",
    "goodbye",
    "bye bye",
    "cheers",
    "that's all",
    "thats all",
    "that's all thank you",
    "thats all thank you",
    "that's everything",
    "thats everything",
    "that will be all",
    "that'll be all",
    "thatll be all",
    "nothing else",
    "nothing else thanks",
    "nothing else thank you",
    "i'm good",
    "im good",
    "all good",
}

# Neutral filler fragments allowed inside a multi-part closing turn
# (e.g. "no, that's all" / "okay, thank you, bye").
_CLOSING_ACK_FILLER = {
    "no",
    "ok",
    "okay",
    "alright",
    "yeah",
    "yes",
    "great",
    "perfect",
    "lovely",
}

# Closing fragments that conclusively end the conversation: once the caller says
# the business is finished, the agent should say goodbye and end the call rather
# than waiting for the idle timeout. Bare thanks ("thank you") stays non-terminal.
_CONCLUSIVE_CLOSING_PHRASES = {
    "that's all",
    "thats all",
    "that's all thank you",
    "thats all thank you",
    "that's everything",
    "thats everything",
    "that will be all",
    "that'll be all",
    "thatll be all",
    "nothing else",
    "nothing else thanks",
    "nothing else thank you",
    "i'm good",
    "im good",
    "all good",
    "bye",
    "goodbye",
    "bye bye",
    "thank you bye",
    "thanks bye",
}

_ASSISTANT_SIGN_OFF_CUES = {
    "all set",
    "bye for now",
    "goodbye",
    "have a good day",
    "have a great day",
    "have a lovely day",
    "look forward to seeing you",
    "see you then",
    "speak soon",
    "take care",
    "that's everything",
    "thats everything",
    "we're all set",
    "were all set",
    "you're all set",
    "youre all set",
}

_ASSISTANT_STILL_SOLICITING_CUES = {
    "anything else",
    "can i help",
    "do you need",
    "is there",
}

_LANGUAGE_PREFERENCE_PHRASES = {
    "can we speak english",
    "could we speak english",
    "english please",
    "i speak english",
    "please speak english",
    "speak english",
}

_NON_ENGLISH_ASSISTANT_MARKERS = {
    # German drift observed in live Clinic Recall calls.
    " bitte ",
    " der ",
    " die ",
    " ich ",
    " mit ",
    " möchte ",
    " sind ",
    " spreche ",
    " und ",
    # Turkish drift observed in live inbound clinic calls (2026-07-07).
    " merhaba ",
    " nasıl ",
    " yardımcı ",
    " size ",
    " lütfen ",
    " olabilirim ",
    " istiyorsunuz ",
    # Common Spanish/French drift markers.
    " hola ",
    " gracias ",
    " cómo ",
    " usted ",
    " bonjour ",
    " merci ",
    " comment ",
    " vous ",
}

# Characters that never appear in the assistant's English (en-GB) replies but are
# common in observed drift languages (Turkish dotless-i/ğ/ş, Spanish inverted
# punctuation/ñ). One hit is a strong non-English signal on its own.
_NON_ENGLISH_CHAR_PATTERN = re.compile(r"[ığşİ¿¡ñ]")

_ENGLISH_RECOVERY_INSTRUCTION = (
    "Respond only in English (en-GB). Briefly apologise for the language switch, "
    "then repeat your last assistant message in English."
)

# Exact polite close for caller-initiated goodbyes. Spoken deterministically on
# capable transports so the caller always hears a warm close before hang-up.
_USER_GOODBYE_LINE = "Say exactly: Thanks for calling. Take care, goodbye."
_VOICE_OPT_OUT_RECORDED_LINE = (
    "Say exactly: Your request to stop calls has been recorded. We won't continue "
    "this booking conversation. Goodbye."
)
_VOICE_OPT_OUT_FAILED_LINE = (
    "Say exactly: I couldn't record your request to stop calls just now. Please contact "
    "the clinic directly to process it. I won't continue this booking conversation. Goodbye."
)
_VOICE_IDENTITY_POLICY_UNAVAILABLE_LINE = (
    "Say exactly: I can't verify identity on this call, so I won't discuss patient "
    "or appointment details. The clinic team will follow up. Goodbye."
)

_BOOKING_TOOLS = {"book_slot", "reschedule"}
_CONFIRMATION_TOOLS = {"send_sms", "send_email"}

# Hard-stop reasons end the call after their governed close. Nonurgent clinical
# concerns are escalated without advice, but the caller may continue with safe
# appointment or clinic logistics (live staging call 2026-07-10).
_TERMINAL_ESCALATION_REASONS = {"urgent", "complaint", "safeguarding", "distress"}
_MODEL_TERMINAL_ESCALATION_REASONS = {
    "clinical",
    "urgent",
    "complaint",
    "safeguarding",
    "distress",
}

# Non-urgent escalation reasons where a captured booking request keeps the inbound
# call open: the caller asked for an appointment, so the agent confirms the staff
# escalation AND the captured booking request, then continues the call gracefully
# instead of hanging up. Urgent/safeguarding/distress/complaint still fail closed.
_BOOKING_CONTINUABLE_ESCALATION_REASONS = {"clinical"}

# Turns carrying instruction-override or advice-demand cues stay fail-closed even
# when they mention a booking: the caller is trying to steer the agent, not
# describing symptoms while asking for an appointment.
_INSTRUCTION_OVERRIDE_CUES = {
    "ignore previous instructions",
    "ignore your instructions",
    "ignore all previous",
    "ignore the classifier",
    "disregard your instructions",
    "disregard previous",
    "pretend you are",
    "pretend to be",
    "system prompt",
    "developer mode",
    "give medication advice",
    "give medical advice",
    "give me medical advice",
}

_IDENTITY_ANSWER_PREFIXES = {
    "my name is ",
    "my name's ",
    "this is ",
}

_COMPLAINT_OR_NEGATIVE_TERMS = {
    "complain",
    "complaint",
    "awful",
    "terrible",
    "poor",
    "bad",
    "unhappy",
    "rude",
    "not happy",
    "worse",
    "disappointed",
    "upset",
    "angry",
    "unacceptable",
}

# Explicit complaint cues only — NOT generic negative adjectives. Live call
# 2026-07-07 (CAc04129d): "I'm having a TERRIBLE cough and I want to schedule an
# appointment" matched the broad adjective set above and was escalated as a
# terminal complaint, dropping a clinical+booking call that should have stayed
# open. A CLINICAL-intent turn is only a complaint when one of these cues is
# present; the broad set stays unchanged for its other defensive call sites.
_EXPLICIT_COMPLAINT_TERMS = {
    "complain",
    "complaint",
    "unacceptable",
    "unhappy",
    "disappointed",
    "angry",
    "rude",
}

# The OBJECT of an explicit complaint decides complaint-vs-clinical routing.
# Live call dc64f52c (2026-07-10): "complain about a cough and headache" is a
# symptom description spoken with the word "complain", not a service
# complaint — the terminal complaint route hung up on a caller who had asked
# to book. When the complaint's object is clinical/urgent content with no
# service target, the turn routes clinical (booking-continuable, polite
# close). Word-boundary matching only: classify_intent's substring matching
# sees "pain" inside "complaint", so reusing it here would let "complain
# about my complaint" bypass the terminal complaint gate.
_COMPLAINT_OBJECT_PATTERN = re.compile(
    r"\bcomplain(?:t|ts|ing|ed)?\s+(?:about|regarding|of|over|that)\s+(.{1,160})"
)
# Negated complaint mentions ("I'm not complaining, but…") are not complaints.
_NEGATED_COMPLAINT_PATTERN = re.compile(r"\b(?:not|no|never)\s+(?:a\s+)?complain\w*")
_CLINICAL_COMPLAINT_OBJECT_TERMS = frozenset(CLINICAL_TERMS | URGENT_TERMS)
# Service targets keep an explicit complaint terminal even when symptoms are
# also mentioned ("complain about my doctor ignoring my cough").
_SERVICE_COMPLAINT_TARGET_TERMS = frozenset(
    {
        "clinic",
        "practice",
        "surgery",
        "staff",
        "reception",
        "receptionist",
        "doctor",
        "doctors",
        "clinician",
        "clinicians",
        "nurse",
        "nurses",
        "gp",
        "service",
        "services",
        "care",
        "treatment",
        "appointment",
        "appointments",
        "booking",
        "wait",
        "waiting",
        "experience",
        "handled",
        "handling",
        "rude",
        "phone",
        "line",
    }
)

_SAFEGUARDING_TERMS = {
    "abuse",
    "abused",
    "neglect",
    "neglected",
    "safeguarding",
    "unsafe at home",
    "not safe at home",
}

_BARE_IDENTITY_REJECT_TERMS = {
    "maybe",
    "later",
    "unsure",
    "unknown",
    "question",
    "appointment",
    "book",
    "rebook",
    "cancel",
    "stop",
    "don't",
    "dont",
    "do not",
}

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION ORCHESTRATOR REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

# Module-level registry for VoiceLive orchestrators (per session)
# This enables scenario updates to reach active VoiceLive sessions
# Uses standard dict but includes cleanup of stale entries
_voicelive_orchestrators: dict[str, LiveOrchestrator] = {}
_registry_lock = asyncio.Lock()


def register_voicelive_orchestrator(session_id: str, orchestrator: LiveOrchestrator) -> None:
    """Register a VoiceLive orchestrator for scenario updates."""
    # Clean up stale entries first (orchestrators that may have been orphaned)
    _cleanup_stale_orchestrators()
    _voicelive_orchestrators[session_id] = orchestrator
    logger.debug(
        "Registered VoiceLive orchestrator | session=%s registry_size=%d",
        session_id,
        len(_voicelive_orchestrators),
    )


def unregister_voicelive_orchestrator(session_id: str) -> None:
    """Unregister a VoiceLive orchestrator when session ends."""
    orchestrator = _voicelive_orchestrators.pop(session_id, None)
    if orchestrator:
        logger.debug(
            "Unregistered VoiceLive orchestrator | session=%s registry_size=%d",
            session_id,
            len(_voicelive_orchestrators),
        )


def get_voicelive_orchestrator(session_id: str) -> LiveOrchestrator | None:
    """Get the VoiceLive orchestrator for a session."""
    return _voicelive_orchestrators.get(session_id)


def _cleanup_stale_orchestrators() -> int:
    """
    Clean up orchestrators that are no longer valid.

    This catches cases where sessions ended without proper cleanup.
    Returns the number of stale entries removed.
    """
    stale_keys = []
    for session_id, orchestrator in list(_voicelive_orchestrators.items()):
        # Check if orchestrator is still valid (has connection reference)
        if orchestrator.conn is None and orchestrator.agents == {}:
            stale_keys.append(session_id)

    for key in stale_keys:
        _voicelive_orchestrators.pop(key, None)

    if stale_keys:
        logger.debug(
            "Cleaned up %d stale orchestrators from registry | remaining=%d",
            len(stale_keys),
            len(_voicelive_orchestrators),
        )

    return len(stale_keys)


def get_orchestrator_registry_size() -> int:
    """Get current size of orchestrator registry (for monitoring)."""
    return len(_voicelive_orchestrators)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════


async def _auto_load_user_context(system_vars: dict[str, Any]) -> None:
    """
    Auto-load user profile into system_vars if client_id is present but session_profile is missing.

    This ensures that agents receiving handoffs with client_id can access user context
    for personalized conversations, even if the originating agent didn't pass full profile.

    Modifies system_vars in-place.
    """
    if system_vars.get("session_profile"):
        # Already have session_profile, no need to load
        return

    client_id = system_vars.get("client_id")
    if not client_id:
        # Check handoff_context for client_id
        handoff_ctx = system_vars.get("handoff_context", {})
        client_id = handoff_ctx.get("client_id") if isinstance(handoff_ctx, dict) else None

    if not client_id:
        return

    try:
        profile = await load_user_profile_by_client_id(client_id)
        if profile:
            system_vars["session_profile"] = profile
            system_vars["client_id"] = profile.get("client_id", client_id)
            system_vars["customer_intelligence"] = profile.get("customer_intelligence", {})
            system_vars["caller_name"] = profile.get("full_name")
            if profile.get("institution_name"):
                system_vars.setdefault("institution_name", profile["institution_name"])
            logger.info(
                "🔄 Auto-loaded user context for handoff | client_id=%s name=%s",
                client_id,
                profile.get("full_name"),
            )
    except Exception as exc:
        logger.warning("Failed to auto-load user context: %s", exc)


def _session_contract_for_log(session_obj: Any, model_name: str) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    as_dict = getattr(session_obj, "as_dict", None)
    if callable(as_dict):
        try:
            candidate = as_dict()
            if isinstance(candidate, dict):
                raw = candidate
        except Exception:
            logger.debug("Failed to serialize session.updated contract", exc_info=True)

    def field(name: str) -> Any:
        value = raw.get(name)
        if value is None:
            value = getattr(session_obj, name, None)
        value_as_dict = getattr(value, "as_dict", None)
        if callable(value_as_dict):
            try:
                return value_as_dict()
            except Exception:
                logger.debug("Failed to serialize session field %s", name, exc_info=True)
        return value

    return {
        "model": field("model") or model_name,
        "voice": field("voice"),
        "turn_detection": field("turn_detection"),
        "input_audio_transcription": field("input_audio_transcription"),
        "input_audio_echo_cancellation": field("input_audio_echo_cancellation"),
        "input_audio_noise_reduction": field("input_audio_noise_reduction"),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════


class LiveOrchestrator:
    """
    Orchestrates agent switching and tool execution for VoiceLive multi-agent system.

    All tool execution flows through the shared tool registry for centralized management:
    - Handoff tools → trigger agent switching
    - Business tools → execute and return results to model

    GenAI Telemetry:
    - Emits invoke_agent spans for App Insights Agents blade
    - Tracks token usage per agent session
    - Records LLM TTFT (Time To First Token) metrics
    """

    def __init__(
        self,
        conn,
        agents: dict[str, UnifiedAgent],
        handoff_map: dict[str, str] | None = None,
        start_agent: str = "Concierge",
        audio_processor=None,
        messenger=None,
        call_connection_id: str | None = None,
        *,
        transport: str = "acs",
        model_name: str | None = None,
        memo_manager: MemoManager | None = None,
    ):
        self.conn = conn
        self.agents = agents
        self._handoff_map = handoff_map or {}
        self.active = start_agent
        self.audio = audio_processor
        self.messenger = messenger
        self._model_name = model_name or "gpt-4o-realtime"
        self.visited_agents: set = set()
        self._pending_greeting: str | None = None
        self._pending_greeting_agent: str | None = None
        # Bounded deque to preserve last N user utterances for better handoff context
        self._user_message_history: deque[str] = deque(maxlen=5)
        self._last_user_message: str | None = None  # Keep for backward compatibility
        # Track assistant responses for conversation history persistence
        self._last_assistant_message: str | None = None
        self.call_connection_id = call_connection_id
        self._call_center_triggered = False
        self._transport = transport
        self._greeting_tasks: set[asyncio.Task] = set()
        self._active_response_id: str | None = None
        # Serialize + coalesce Clinic Recall safety responses. Live call 2026-07-07
        # (CAda614): two ambiguous escalations 5s apart each did response.cancel()
        # + response.create(); the second create raced the first response and both
        # died silently (dead air). The lock prevents interleaved cancel/create and
        # the in-flight flag makes a second escalation reuse the pending spoken
        # response instead of killing it.
        self._safety_response_lock: asyncio.Lock = asyncio.Lock()
        self._safety_response_inflight: bool = False
        # Deterministic outcome-free ack for mixed clinical+booking turns (plan
        # 2026-07-09): the ack speaks immediately while the escalation + booking
        # writes run, and the outcome-referencing final line is deferred here
        # until the ack's RESPONSE_DONE. Never a second queue: one pending
        # instruction, superseded by any direct safety response.
        self._pending_safety_final_instruction: str | None = None
        # True while the outcome-free ACK (not a full safety response) is the
        # in-flight safety utterance. A direct safety response must supersede
        # the ack (cancel+create) instead of coalescing into it — coalescing is
        # only correct between two full safety responses.
        self._safety_ack_inflight: bool = False
        # True between INPUT_AUDIO_BUFFER_SPEECH_STARTED and _SPEECH_STOPPED.
        # Deferred responses must not be created while the caller is talking.
        self._user_speech_active: bool = False
        self._system_vars: dict[str, Any] = {}
        # Flag to prevent SESSION_UPDATED from cancelling handoff-triggered responses
        self._handoff_response_pending: bool = False

        # Scenario switch flag — prevents _sync_from_memo_manager from overwriting
        # self.active with stale MemoManager data after an explicit scenario switch
        self._scenario_switch_pending: bool = False

        # Track pending tool outputs to batch them before calling response.create()
        # When model makes multiple tool calls, we queue results and trigger ONE response
        self._pending_tool_outputs: list[tuple[str, str]] = []  # [(call_id, output_json), ...]
        self._response_had_tool_calls: bool = False
        self._response_done_epoch: int = 0
        self._async_tool_tasks: set[asyncio.Task] = set()
        self._tool_completion_lock: asyncio.Lock = asyncio.Lock()
        self._completed_tool_outputs_for_followup: list[tuple[str, str]] = []
        self._post_tool_response_pending: bool = False
        self._post_tool_response_interrupted: bool = False
        self._post_tool_response_instruction: str | None = None
        # Live dialogue phase for the Clinic Recall outbound flow. Drives phase-aware
        # safety routing (e.g. a name answer during the identity phase is expected,
        # not an ambiguous escalation). Inferred from assistant cues + tool calls.
        self._call_phase: str = "greeting"
        self._clinic_recall_booking_succeeded = False
        self._inbound_booking_request_created = False
        self._inbound_escalation_reasons_created: set[str] = set()
        self._awaiting_inbound_availability_confirmation = False
        self._pending_booking_end = False
        self._booking_end_requested = False
        self._call_outcome_emitted = False
        self._pending_english_recovery = False
        self._english_recovery_requested = False
        # Consecutive 1-2 word unintelligible turns. The first gets one exact
        # clarification instead of a spurious ambiguous escalation (live call
        # dc64f52c: ASR noise "Ewa" created a staff task); the second fails
        # closed to the ambiguous escalation. Any longer turn resets it.
        self._consecutive_noise_turns = 0

        # MemoManager for session state continuity (consistent with CascadeOrchestratorAdapter)
        self._memo_manager: MemoManager | None = memo_manager

        # Unified metrics tracking (tokens, TTFT, turn count)
        self._metrics = OrchestratorMetrics(
            agent_name=start_agent,
            call_connection_id=call_connection_id,
            session_id=getattr(messenger, "session_id", None) if messenger else None,
        )

        # Throttle session context updates to avoid hot path latency
        self._last_session_update_time: float = 0.0
        self._session_update_min_interval: float = 2.0  # Min seconds between updates
        self._pending_session_update: bool = False

        if self.messenger:
            try:
                self.messenger.set_active_agent(self.active)
            except AttributeError:
                logger.debug("Messenger does not support set_active_agent", exc_info=True)

        # Use case-insensitive lookup for start agent validation
        actual_key, _ = find_agent_by_name(self.agents, self.active)
        if actual_key is None:
            raise ValueError(f"Start agent '{self.active}' not found in registry")
        # Normalize active to the actual key in agents dict
        self.active = actual_key

        # Initialize the tool registry
        initialize_tools()

        # Initialize HandoffService for unified handoff resolution
        self._handoff_service: HandoffService | None = None

        # Sync state from MemoManager if available
        if self._memo_manager:
            self._sync_from_memo_manager()

    # ═══════════════════════════════════════════════════════════════════════════
    # MEMO MANAGER SYNC (consistent with CascadeOrchestratorAdapter)
    # ═══════════════════════════════════════════════════════════════════════════

    @property
    def memo_manager(self) -> MemoManager | None:
        """Return the current MemoManager instance."""
        return self._memo_manager

    @property
    def _session_id(self) -> str | None:
        """
        Get the session ID from memo_manager or messenger.

        Cached property to avoid repeated attribute access.
        """
        if self._memo_manager:
            session_id = getattr(self._memo_manager, "session_id", None)
            if session_id:
                return session_id
        if self.messenger:
            return getattr(self.messenger, "session_id", None)
        return None

    @property
    def _orchestrator_config(self):
        """
        Get cached orchestrator config for scenario resolution.

        Lazily resolves and caches the config on first access to avoid
        repeated calls to resolve_orchestrator_config() during the session.

        The config is cached per-instance (session lifetime), which is appropriate
        because scenario changes during a call would be disruptive anyway.
        """
        if not hasattr(self, "_cached_orchestrator_config"):
            from apps.artagent.backend.voice.shared.config_resolver import (
                resolve_orchestrator_config,
            )

            self._cached_orchestrator_config = resolve_orchestrator_config(
                session_id=self._session_id
            )
            logger.debug(
                "[LiveOrchestrator] Cached orchestrator config | scenario=%s session=%s",
                self._cached_orchestrator_config.scenario_name,
                self._session_id,
            )
        return self._cached_orchestrator_config

    def _sync_from_memo_manager(self) -> None:
        """
        Sync orchestrator state from MemoManager.
        Called at initialization and optionally at turn boundaries.

        Uses shared sync_state_from_memo for consistency with CascadeOrchestratorAdapter.
        
        NOTE: For VoiceLive, we intentionally DO NOT sync visited_agents because:
        - VoiceLive starts with a fresh conversation history each connection
        - If we sync visited_agents, we'd show return_greeting but model has no context
        - This causes the model to behave inconsistently (greeting says "welcome back" 
          but model doesn't know what happened before)
        """
        if not self._memo_manager:
            return

        # Use shared sync utility
        state = sync_state_from_memo(
            self._memo_manager,
            available_agents=set(self.agents.keys()),
        )

        # Apply synced state - but NOT visited_agents for VoiceLive
        # VoiceLive conversation history is per-connection, so we always treat as first visit
        if self._scenario_switch_pending:
            # Scenario switch is authoritative — write adapter's active agent to MemoManager
            logger.info(
                "[LiveOrchestrator] Scenario switch pending — writing active to MemoManager | active=%s memo_active=%s",
                self.active,
                state.active_agent,
            )
            sync_state_to_memo(self._memo_manager, active_agent=self.active)
            self._scenario_switch_pending = False
        elif state.active_agent:
            self.active = state.active_agent
            logger.debug("[LiveOrchestrator] Synced active_agent: %s", self.active)

        # IMPORTANT: Do NOT sync visited_agents for VoiceLive
        # Each VoiceLive connection starts fresh - syncing visited_agents causes
        # return_greeting to be used but model has no conversation context
        # if state.visited_agents:
        #     self.visited_agents = state.visited_agents
        #     logger.debug("[LiveOrchestrator] Synced visited_agents: %s", self.visited_agents)
        logger.debug(
            "[LiveOrchestrator] Skipping visited_agents sync - VoiceLive starts fresh each connection"
        )

        if state.system_vars:
            self._system_vars.update(state.system_vars)
            logger.debug("[LiveOrchestrator] Synced system_vars")

        # Restore user message history if available (for session continuity)
        try:
            stored_history = self._memo_manager.get_value_from_corememory("user_message_history")
            if stored_history and isinstance(stored_history, list):
                self._user_message_history = deque(stored_history, maxlen=5)
                if stored_history:
                    self._last_user_message = stored_history[-1]
                logger.debug(
                    "[LiveOrchestrator] Restored %d messages from history",
                    len(stored_history),
                )
        except Exception:
            logger.debug("Failed to restore user message history", exc_info=True)

        # Handle pending handoff if any
        if state.pending_handoff:
            target = state.pending_handoff.get("target_agent")
            if target and target in self.agents:
                logger.info("[LiveOrchestrator] Pending handoff detected: %s", target)
                self.active = target
                # Clear the pending handoff
                sync_state_to_memo(
                    self._memo_manager, active_agent=self.active, clear_pending_handoff=True
                )

    def _sync_to_memo_manager(self) -> None:
        """
        Sync orchestrator state back to MemoManager.
        Called at turn boundaries to persist state.

        Uses shared sync_state_to_memo for consistency with CascadeOrchestratorAdapter.
        """
        if not self._memo_manager:
            return

        # Use shared sync utility
        sync_state_to_memo(
            self._memo_manager,
            active_agent=self.active,
            visited_agents=self.visited_agents,
            system_vars=self._system_vars,
        )

        # Sync last user message (VoiceLive-specific) for backward compatibility
        if hasattr(self._memo_manager, "last_user_message") and self._last_user_message:
            self._memo_manager.last_user_message = self._last_user_message

        # Persist user message history for session continuity
        if self._user_message_history:
            try:
                self._memo_manager.set_corememory(
                    "user_message_history", list(self._user_message_history)
                )
            except Exception:
                logger.debug("Failed to persist user message history", exc_info=True)

        logger.debug("[LiveOrchestrator] Synced state to MemoManager")

    def cleanup(self) -> None:
        """
        Clean up orchestrator resources to prevent memory leaks.

        This should be called when the VoiceLive session ends. It:
        - Cancels all pending greeting tasks
        - Clears references to agents and connections
        - Clears user message history deque
        - Resets all stateful tracking variables

        Note: This method is synchronous and does not await any coroutines.
        For async cleanup, use the handler's stop() method which calls this.
        """
        # Cancel all pending greeting tasks
        self._cancel_pending_greeting_tasks()
        for task in list(self._async_tool_tasks):
            task.cancel()
        self._async_tool_tasks.clear()

        # Clear agents registry reference
        self.agents = {}
        self._handoff_map = {}

        # Clear connection reference (do not close - handler owns it)
        self.conn = None

        # Clear messenger reference to break circular refs
        self.messenger = None
        self.audio = None

        # Clear memo manager reference (handler/endpoint owns lifecycle)
        self._memo_manager = None

        # Clear handoff service
        self._handoff_service = None

        # Clear user message history
        self._user_message_history.clear()
        self._last_user_message = None
        self._last_assistant_message = None
        self._inbound_escalation_reasons_created.clear()

        # Clear pending greeting state
        self._pending_greeting = None
        self._pending_greeting_agent = None

        # Reset tracking variables
        self._active_response_id = None
        self._system_vars.clear()
        self.visited_agents.clear()

        logger.debug("[LiveOrchestrator] Cleanup complete")

    def update_scenario(
        self,
        agents: dict[str, UnifiedAgent],
        handoff_map: dict[str, str],
        start_agent: str | None = None,
        scenario_name: str | None = None,
    ) -> None:
        """
        Update the orchestrator with a new scenario configuration.

        This is called when the user changes scenarios mid-session via the UI.
        The orchestrator's agents and handoff map are updated to reflect
        the new scenario without restarting the VoiceLive connection.

        Args:
            agents: New UnifiedAgent registry (no adapter needed)
            handoff_map: New handoff routing map
            start_agent: Optional new start agent to switch to
            scenario_name: Optional scenario name for logging
        """
        old_agents = list(self.agents.keys())
        old_active = self.active
        needs_session_update = False

        # Update agents registry
        self.agents = agents

        # Update handoff map
        self._handoff_map = handoff_map

        # Clear cached HandoffService so it's recreated with new scenario
        self._handoff_service = None

        # Clear cached orchestrator config so it's resolved with new scenario
        # CRITICAL: Without this, _update_session_context() uses the OLD cached config
        # and injects the wrong handoff instructions for the new scenario
        if hasattr(self, "_cached_orchestrator_config"):
            delattr(self, "_cached_orchestrator_config")

        # Clear visited agents for fresh scenario experience
        self.visited_agents.clear()

        # Always switch to start_agent when a new scenario is explicitly selected
        if start_agent:
            if start_agent != self.active:
                self.active = start_agent
                needs_session_update = True
                logger.info(
                    "🔄 VoiceLive switching to scenario start_agent | from=%s to=%s scenario=%s",
                    old_active,
                    start_agent,
                    scenario_name or "(unknown)",
                )
            else:
                # Same agent but scenario changed - still need to update session
                needs_session_update = True
        elif self.active not in agents:
            # Current agent not in new scenario - switch to first available
            available = list(agents.keys())
            if available:
                self.active = available[0]
                needs_session_update = True
                logger.warning(
                    "🔄 VoiceLive current agent not in scenario, switching | from=%s to=%s",
                    old_active,
                    self.active,
                )

        logger.info(
            "🔄 VoiceLive scenario updated | old_agents=%s new_agents=%s active=%s scenario=%s",
            old_agents,
            list(agents.keys()),
            self.active,
            scenario_name or "(unknown)",
        )

        # Mark scenario switch pending so _sync_from_memo_manager doesn't
        # overwrite self.active with stale data from a previous MemoManager snapshot
        self._scenario_switch_pending = True

        # CRITICAL: Trigger a session update to apply the new agent's instructions
        # This ensures VoiceLive uses the correct system prompt for the new agent
        if needs_session_update:
            self._schedule_scenario_session_update()

    def _schedule_scenario_session_update(self) -> None:
        """
        Schedule a full agent session update after scenario change.

        This applies the new agent's complete session configuration (voice, tools,
        VAD, instructions) - not just instructions. This is critical for scenario
        switches to take effect properly in VoiceLive.

        This runs in the background to avoid blocking the scenario update call.
        """
        async def _do_update():
            try:
                agent = self.agents.get(self.active)
                if not agent:
                    logger.warning(
                        "🔄 VoiceLive scenario update failed - agent not found | agent=%s",
                        self.active,
                    )
                    return

                # Build system vars for the new agent
                system_vars = dict(self._system_vars)
                system_vars["active_agent"] = self.active

                # Get session_id for the apply call
                session_id = self._session_id

                # CRITICAL: Apply the FULL agent session config, not just instructions
                # This includes voice, tools, VAD settings, etc.
                # This is the same as what _switch_to() does during handoffs
                await agent.apply_voicelive_session(
                    self.conn,
                    system_vars=system_vars,
                    say=None,  # Don't trigger a greeting on scenario switch
                    session_id=session_id,
                    call_connection_id=self.call_connection_id,
                )

                # Update messenger's active agent
                if self.messenger:
                    try:
                        self.messenger.set_active_agent(self.active)
                    except AttributeError:
                        pass

                logger.info(
                    "🔄 VoiceLive session fully updated for scenario change | agent=%s session=%s",
                    self.active,
                    session_id,
                )
            except Exception:
                logger.warning("Failed to update session after scenario change", exc_info=True)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Cannot schedule session update - no event loop available")
            return
        loop.create_task(_do_update())

    async def _inject_conversation_history(self) -> None:
        """
        Inject conversation history as text items into VoiceLive conversation.

        CRITICAL FOR CONTEXT RETENTION:
        VoiceLive processes audio natively, but the model can "forget" context
        between turns. By injecting the conversation history as explicit text
        items, we give the model concrete text to reference.

        This should be called:
        - After session.update on agent switch (_switch_to)
        - Before the first response is triggered

        The text items become part of the conversation context that the model
        sees for all subsequent responses.
        """
        if not self.conn or not self._user_message_history:
            return

        try:
            # Inject each historical user message as a text conversation item
            # This establishes explicit text context for the model
            for msg in self._user_message_history:
                if not msg or not msg.strip():
                    continue
                
                # Create user message item with text content
                text_part = InputTextContentPart(text=msg)
                user_item = UserMessageItem(content=[text_part])
                
                # Add to conversation
                await self.conn.conversation.item.create(item=user_item)
            
            # Also inject last assistant message if available
            if self._last_assistant_message:
                # Create assistant message with text content
                text_part = OutputTextContentPart(text=self._last_assistant_message)
                assistant_item = AssistantMessageItem(content=[text_part])
                await self.conn.conversation.item.create(item=assistant_item)

            logger.info(
                "[LiveOrchestrator] Injected %d conversation items for context",
                len(self._user_message_history) + (1 if self._last_assistant_message else 0),
            )
        except Exception:
            logger.debug("Failed to inject conversation history", exc_info=True)

    def _refresh_session_context(self) -> None:
        """
        Refresh session context from MemoManager at the start of each turn.

        This picks up any external updates such as:
        - CRM lookups completed by tools
        - Session profile updates from MFA verification
        - Slot values filled by previous turns
        - Tool outputs from business logic

        Called from _handle_transcription_completed to ensure each turn
        has fresh context for prompt rendering.
        """
        if not self._memo_manager:
            return

        try:
            # Refresh session profile if updated externally
            session_profile = self._memo_manager.get_value_from_corememory("session_profile")
            if session_profile and isinstance(session_profile, dict):
                # Update system_vars with fresh profile data
                self._system_vars["session_profile"] = session_profile
                self._system_vars["client_id"] = session_profile.get("client_id")
                self._system_vars["caller_name"] = session_profile.get("full_name")
                self._system_vars["customer_intelligence"] = session_profile.get(
                    "customer_intelligence", {}
                )
                if session_profile.get("institution_name"):
                    self._system_vars["institution_name"] = session_profile["institution_name"]

            # Refresh slots (collected information from previous turns)
            slots = self._memo_manager.get_context("slots", {})
            if slots:
                self._system_vars["slots"] = slots
                self._system_vars["collected_information"] = slots

            # Refresh tool outputs for context continuity
            tool_outputs = self._memo_manager.get_context("tool_outputs", {})
            if tool_outputs:
                self._system_vars["tool_outputs"] = tool_outputs

            logger.debug("[LiveOrchestrator] Refreshed session context from MemoManager")
        except Exception:
            logger.debug("Failed to refresh session context", exc_info=True)

    async def _update_session_context(self) -> None:
        """
        Update VoiceLive session instructions with current context.

        This is called BEFORE each model response to ensure the model's instructions
        reflect the latest conversation context. Without this, the realtime model
        tends to forget what was discussed in previous turns.

        The update includes:
        - Base agent instructions (from prompt template)
        - Explicit conversation recap (critical for context retention)
        - Collected slots (e.g., user's name, account info)
        - Tool outputs (e.g., CRM lookup results)
        """
        if not self.conn or not self.active:
            return

        agent = self.agents.get(self.active)
        if not agent:
            return

        try:
            # Build context for prompt rendering
            context_vars = dict(self._system_vars)
            context_vars["active_agent"] = self.active

            # Add conversation context from message history
            if self._user_message_history:
                context_vars["recent_user_messages"] = list(self._user_message_history)
                if len(self._user_message_history) > 1:
                    context_vars["conversation_summary"] = " → ".join(self._user_message_history)

            # Add last assistant response for context continuity
            if self._last_assistant_message:
                context_vars["last_assistant_response"] = self._last_assistant_message

            # Render base instructions from agent prompt template
            base_instructions = agent._agent.render_prompt(context_vars) or ""

            # Inject handoff instructions from scenario configuration
            # Use the cached orchestrator config (supports both file-based and session-scoped)
            config = self._orchestrator_config
            if config.scenario and agent._agent.name:
                # Use scenario.build_handoff_instructions directly (works for session scenarios)
                handoff_instructions = config.scenario.build_handoff_instructions(agent._agent.name)
                if handoff_instructions:
                    base_instructions = f"{base_instructions}\n\n{handoff_instructions}" if base_instructions else handoff_instructions
                    logger.info(
                        "[LiveOrchestrator] Injected handoff instructions | agent=%s scenario=%s len=%d",
                        agent._agent.name,
                        config.scenario_name,
                        len(handoff_instructions),
                    )
            else:
                logger.debug(
                    "[LiveOrchestrator] No scenario or agent name for handoff instructions | scenario=%s agent=%s",
                    config.scenario_name if config.scenario else None,
                    agent._agent.name if hasattr(agent, '_agent') else None,
                )

            # Build conversation recap to append to instructions
            # This is critical for realtime models which tend to forget context
            conversation_recap = self._build_conversation_recap()

            # Combine base instructions with conversation recap
            if conversation_recap:
                updated_instructions = f"{base_instructions}\n\n{conversation_recap}"
            else:
                updated_instructions = base_instructions

            if not updated_instructions:
                return

            # Update session with new instructions
            from azure.ai.voicelive.models import RequestSession

            await self.conn.session.update(
                session=RequestSession(instructions=updated_instructions)
            )

            logger.debug(
                "[LiveOrchestrator] Updated session | agent=%s history_len=%d slots=%s",
                self.active,
                len(self._user_message_history),
                list(context_vars.get("slots", {}).keys()) if context_vars.get("slots") else [],
            )
        except Exception:
            logger.debug("Failed to update session context", exc_info=True)

    async def apply_live_session_settings(
        self,
        *,
        turn_detection: dict[str, Any] | None = None,
        voice: dict[str, Any] | None = None,
    ) -> bool:
        """
        Push VAD / voice tweaks to the live VoiceLive connection without a reconnect.

        VoiceLive supports partial ``session.update`` for ``turn_detection`` and
        ``voice`` (the generative model is the only thing bound at connect()).
        The active per-session agent's stored config is also mutated so the change
        survives subsequent full session updates (e.g. on the next agent switch).

        Returns True if an update was pushed, False if nothing live to update.
        """
        if not self.conn or not self.active:
            return False
        agent = self.agents.get(self.active)
        if not agent:
            return False
        ua = getattr(agent, "_agent", agent)

        # Mutate the per-session agent so the tweak persists across turns.
        if turn_detection:
            sess = dict(ua.session or {})
            td = dict(sess.get("turn_detection") or {})
            for key in ("type", "threshold", "silence_duration_ms", "prefix_padding_ms"):
                if turn_detection.get(key) is not None:
                    td[key] = turn_detection[key]
            sess["turn_detection"] = td
            ua.session = sess
        if voice and ua.voice is not None:
            if voice.get("name"):
                ua.voice.name = voice["name"]
            if voice.get("rate"):
                ua.voice.rate = voice["rate"]

        try:
            from azure.ai.voicelive.models import RequestSession
        except ImportError:
            logger.warning("VoiceLive SDK unavailable; cannot push live settings")
            return False

        kwargs: dict[str, Any] = {}
        if turn_detection:
            vad = ua.build_voicelive_vad()
            if vad is not None:
                kwargs["turn_detection"] = vad
        if voice:
            voice_payload = ua.build_voicelive_voice()
            if voice_payload is not None:
                kwargs["voice"] = voice_payload

        if not kwargs:
            return False

        await self.conn.session.update(session=RequestSession(**kwargs))
        logger.info(
            "[LiveOrchestrator] Pushed live session settings | agent=%s keys=%s",
            self.active,
            list(kwargs.keys()),
        )
        return True

    def _build_conversation_recap(self) -> str:
        """
        Build an explicit conversation recap to inject into instructions.

        This ensures the realtime model remembers what was discussed,
        even if it tends to forget context between turns.
        """
        parts = []

        # Add conversation history recap
        if self._user_message_history and len(self._user_message_history) > 0:
            parts.append("## CONVERSATION CONTEXT (DO NOT FORGET)")
            parts.append("The user has said the following in this conversation:")
            for i, msg in enumerate(self._user_message_history, 1):
                parts.append(f"  {i}. \"{msg}\"")
            parts.append("")
            parts.append("IMPORTANT: Remember and refer back to what the user has already told you. Do NOT ask them to repeat information they've already provided.")

        # Add collected slots/information
        slots = self._system_vars.get("slots", {})
        if slots:
            parts.append("")
            parts.append("## COLLECTED INFORMATION")
            for key, value in slots.items():
                if value:
                    parts.append(f"  - {key}: {value}")

        # Add last assistant response for context
        if self._last_assistant_message:
            parts.append("")
            parts.append("## YOUR LAST RESPONSE")
            # Truncate if too long
            last_resp = self._last_assistant_message
            if len(last_resp) > 200:
                last_resp = last_resp[:200] + "..."
            parts.append(f'You last said: "{last_resp}"')

        return "\n".join(parts) if parts else ""

    def _schedule_throttled_session_update(self) -> None:
        """
        Schedule a throttled session context update in the background.

        This avoids calling session.update() on the hot path,
        which can add significant latency to each turn.
        The actual network call is performed in a background task.
        """
        now = time.perf_counter()
        elapsed = now - self._last_session_update_time

        # Only update if enough time has passed OR we have a pending update from transcription
        if elapsed < self._session_update_min_interval and not self._pending_session_update:
            logger.debug(
                "[LiveOrchestrator] Skipping session update - throttled (%.1fs < %.1fs)",
                elapsed,
                self._session_update_min_interval,
            )
            return

        self._pending_session_update = False
        self._last_session_update_time = now

        # Refresh context first (fast, local operation)
        self._refresh_session_context()

        # Schedule the actual session update as a background task
        # This prevents blocking the event loop
        async def _do_session_update():
            try:
                await self._update_session_context()
            except Exception:
                logger.debug("Background session update failed", exc_info=True)

        asyncio.create_task(_do_session_update())

    def _schedule_background_sync(self) -> None:
        """
        Schedule MemoManager sync in background to avoid hot path latency.

        The sync is fire-and-forget - failures are logged but don't block.
        """
        if not self._memo_manager:
            return

        def _do_sync():
            try:
                self._sync_to_memo_manager()
            except Exception:
                logger.debug("Background MemoManager sync failed", exc_info=True)

        try:
            asyncio.get_running_loop().call_soon(_do_sync)
        except RuntimeError:
            threading.Thread(target=_do_sync, daemon=True).start()

    # ═══════════════════════════════════════════════════════════════════════════
    # HANDOFF RESOLUTION
    # ═══════════════════════════════════════════════════════════════════════════

    @property
    def handoff_service(self) -> HandoffService:
        """
        Get or create the HandoffService for unified handoff resolution.

        The service is lazily created on first access and uses the cached
        orchestrator config (supports both file-based and session-scoped scenarios).
        """
        if self._handoff_service is None:
            # Use cached orchestrator config for scenario resolution
            config = self._orchestrator_config

            self._handoff_service = HandoffService(
                scenario_name=config.scenario_name,
                handoff_map=self.handoff_map,
                agents=self.agents,
                memo_manager=self._memo_manager,
                scenario=config.scenario,  # Pass scenario object for session-scoped scenarios
            )
        return self._handoff_service

    def get_handoff_target(self, tool_name: str) -> str | None:
        """
        Get the target agent for a handoff tool.

        Uses the static handoff_map. For runtime resolution with
        scenario context, use HandoffService directly.
        """
        return self._handoff_map.get(tool_name)

    @property
    def handoff_map(self) -> dict[str, str]:
        """Get the current handoff map."""
        return self._handoff_map

    # ═══════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ═══════════════════════════════════════════════════════════════════════════

    async def start(self, system_vars: dict | None = None):
        """Apply initial agent session and trigger an intro response."""
        with tracer.start_as_current_span(
            "voicelive_orchestrator.start",
            kind=trace.SpanKind.INTERNAL,
            attributes=create_service_handler_attrs(
                service_name="LiveOrchestrator.start",
                call_connection_id=self.call_connection_id,
                session_id=getattr(self.messenger, "session_id", None) if self.messenger else None,
            ),
        ) as start_span:
            start_span.set_attribute("voicelive.start_agent", self.active)
            start_span.set_attribute("voicelive.agent_count", len(self.agents))
            logger.info("[Orchestrator] Starting with agent: %s", self.active)
            orch_start_ts = time.perf_counter()
            self._system_vars = dict(system_vars or {})
            self._call_phase = "greeting"
            self._clinic_recall_booking_succeeded = False
            self._pending_booking_end = False
            self._booking_end_requested = False
            self._pending_english_recovery = False
            self._english_recovery_requested = False
            
            # Initialize MCP servers for the active agent (non-blocking)
            t0 = time.perf_counter()
            await self._init_mcp_for_agent(self.active)
            mcp_ms = (time.perf_counter() - t0) * 1000
            
            t0 = time.perf_counter()
            await self._switch_to(self.active, self._system_vars)
            switch_ms = (time.perf_counter() - t0) * 1000
            
            total_ms = (time.perf_counter() - orch_start_ts) * 1000
            logger.info(
                "[VoiceLive Startup] orchestrator.start total_ms=%.1f | mcp_init_ms=%.1f switch_to_ms=%.1f | agent=%s",
                total_ms, mcp_ms, switch_ms, self.active,
            )
            start_span.set_attribute("voicelive.orch_start_ms", round(total_ms, 2))
            start_span.set_status(trace.StatusCode.OK)

    async def _init_mcp_for_agent(self, agent_name: str) -> None:
        """
        Initialize MCP server connections for an agent's configured servers.
        
        Connects to MCP servers listed in the agent's mcp_servers field.
        Tools from connected servers become available for the session.
        
        Args:
            agent_name: Name of the agent to initialize MCP for
        """
        if not self._memo_manager:
            return
            
        agent = self.agents.get(agent_name)
        if not agent or not agent.mcp_servers:
            return
            
        try:
            from apps.artagent.backend.registries.toolstore.mcp import get_mcp_configs_for_agent
            
            configs = get_mcp_configs_for_agent(agent.mcp_servers)
            if not configs:
                logger.debug(
                    "[LiveOrchestrator] No MCP servers configured for agent %s",
                    agent_name,
                )
                return
                
            results = await self._memo_manager.init_mcp_servers(configs)
            
            connected = [name for name, success in results.items() if success]
            failed = [name for name, success in results.items() if not success]
            
            if connected:
                logger.info(
                    "[LiveOrchestrator] MCP servers connected for %s: %s",
                    agent_name,
                    connected,
                )
            if failed:
                logger.warning(
                    "[LiveOrchestrator] MCP servers failed for %s: %s",
                    agent_name,
                    failed,
                )
        except Exception as exc:
            logger.warning(
                "[LiveOrchestrator] MCP initialization failed for %s: %s",
                agent_name,
                exc,
            )

    async def handle_event(self, event):
        """Route VoiceLive events to audio + handoff logic."""
        et = event.type

        if et == ServerEventType.SESSION_UPDATED:
            await self._handle_session_updated(event)

        elif et == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            await self._handle_speech_started()

        elif et == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            await self._handle_speech_stopped()

        elif et == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            await self._handle_transcription_completed(event)

        elif et == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED:
            await self._handle_transcription_failed(event)

        elif et == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_DELTA:
            await self._handle_transcription_delta(event)

        elif et == ServerEventType.RESPONSE_AUDIO_DELTA:
            if self.audio:
                await self.audio.queue_audio(event.delta)

        elif et == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
            await self._handle_transcript_delta(event)

        elif et == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            await self._handle_transcript_done(event)

        elif et == ServerEventType.RESPONSE_CREATED:
            response_id = self._response_id_from_event(event)
            if response_id:
                self._active_response_id = response_id

        elif et == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
            call_id = getattr(event, "call_id", None)
            name = getattr(event, "name", None)
            args_json = getattr(event, "arguments", None)
            if self._is_async_business_tool(name):
                self._dispatch_async_business_tool(call_id, name, args_json)
            else:
                await self._execute_tool_call(
                    call_id=call_id,
                    name=name,
                    args_json=args_json,
                )

        elif et == ServerEventType.RESPONSE_DONE:
            await self._handle_response_done(event)

        elif et == ServerEventType.ERROR:
            err = getattr(event, "error", None)
            code = getattr(err, "code", None)
            message = getattr(err, "message", "unknown")
            # Benign cancel-race: a barge-in / response.cancel lands just after the
            # response already finished, so there is no active response to cancel.
            # The handler already suppresses these; mirror that here so we don't
            # emit a noisy duplicate ERROR for an expected condition.
            if code in _BENIGN_ERROR_CODES:
                logger.info(
                    "VoiceLive benign cancel-race ignored | code=%s", code
                )
            else:
                logger.error("VoiceLive error: %s", message)

    # ═══════════════════════════════════════════════════════════════════════════
    # EVENT HANDLERS
    # ═══════════════════════════════════════════════════════════════════════════

    async def _handle_session_updated(self, event) -> None:
        """Handle SESSION_UPDATED event."""
        session_obj = getattr(event, "session", None)
        session_id = getattr(session_obj, "id", "unknown") if session_obj else "unknown"
        session_contract = _session_contract_for_log(session_obj, self._model_name)
        logger.info("Session ready: %s | contract=%s", session_id, session_contract)

        if self.messenger:
            try:
                await self.messenger.send_session_update(
                    agent_name=self.active,
                    session_obj=session_obj,
                    transport=self._transport,
                )
            except Exception:
                logger.debug("Failed to emit session update envelope", exc_info=True)

        # If a handoff response was just triggered, DON'T cancel it
        # The handoff code already called response.create() with the appropriate instructions
        if self._handoff_response_pending:
            logger.debug("[Session Updated] Skipping response.cancel() - handoff response pending")
            self._handoff_response_pending = False
            if self.audio:
                await self.audio.start_capture()
            return

        if self.audio:
            await self.audio.stop_playback()
        try:
            await self.conn.response.cancel()
        except Exception:
            logger.debug("response.cancel() failed during session_ready", exc_info=True)
        if self.audio:
            await self.audio.start_capture()

        if self._pending_greeting and self._pending_greeting_agent == self.active:
            self._cancel_pending_greeting_tasks()
            try:
                await self.agents[self.active].trigger_voicelive_response(
                    self.conn,
                    say=self._pending_greeting,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "[Greeting] Session-ready trigger failed; retrying via fallback", exc_info=True
                )
                self._schedule_greeting_fallback(self.active)
            else:
                self._pending_greeting = None
                self._pending_greeting_agent = None
                # Cold-path latency anchor: greeting response requested. The gap
                # to "[Twilio] First audio delta" is VoiceLive-side generation.
                logger.info(
                    "[Greeting] Session-ready greeting response requested | agent=%s", self.active
                )

    async def _handle_speech_started(self) -> None:
        """Handle user speech started (barge-in)."""
        logger.debug("User speech started → cancel current response")
        self._user_speech_active = True

        # Sync state to MemoManager in background - don't block barge-in response
        # This ensures any partial response context is preserved
        self._schedule_background_sync()
        
        if self.audio:
            await self.audio.stop_playback()
        if self._post_tool_response_pending:
            self._post_tool_response_interrupted = True
        # Only cancel when a response is actually in flight. Cancelling with no
        # active response makes VoiceLive emit a `response_cancel_not_active`
        # server error, which the handler treats as a hard error (StopAudio +
        # UI error) and breaks the next turn. This race widens when VAD fires
        # speech_started right after a turn completes (low silence_duration).
        if self._active_response_id:
            try:
                await self.conn.response.cancel()
            except Exception:
                logger.debug("response.cancel() failed during barge-in", exc_info=True)
        if self.messenger and self._active_response_id:
            try:
                await self.messenger.send_assistant_cancelled(
                    response_id=self._active_response_id,
                    sender=self.active,
                    reason="user_barge_in",
                )
            except Exception:
                logger.debug("Failed to notify assistant cancellation on barge-in", exc_info=True)
        self._active_response_id = None
        # Barge-in kills any in-flight safety response; the next escalation must
        # produce a fresh spoken response instead of coalescing into silence.
        self._safety_response_inflight = False
        self._safety_ack_inflight = False

    async def _handle_speech_stopped(self) -> None:
        """Handle user speech stopped."""
        self._user_speech_active = False
        if self.audio:
            await self.audio.start_playback()

        # Start new turn (increments turn count, resets TTFT tracking)
        turn = self._metrics.start_turn()
        # T0 latency anchor: user finished speaking (VAD end). INFO so the
        # per-turn latency ledger can be rebuilt from AppTraces.
        logger.info("[Turn] Speech stopped | turn=%d agent=%s", turn, self.active)

    async def _handle_transcription_completed(self, event) -> None:
        """Handle user transcription completed."""
        user_transcript = getattr(event, "transcript", "")
        if user_transcript:
            user_text = user_transcript.strip()
            if self._call_phase == "closing":
                logger.debug("[USER] Transcript suppressed during governed close")
                return
            # Ignore empty/noise transcripts (e.g. a bare "." from silence) so they
            # never reach the safety gate and get escalated as ambiguous.
            if not any(ch.isalnum() for ch in user_text):
                logger.debug("[USER] Ignoring empty/non-alphanumeric transcript")
                return
            if len(self._normalise_live_text(user_text).split()) > 2:
                self._consecutive_noise_turns = 0
            if await self._maybe_route_clinic_recall_safety_turn(user_text):
                return
            logger.info("[USER] Says: %s", user_transcript)
            self._last_user_message = user_text
            # Add to bounded history for better handoff context
            self._user_message_history.append(user_text)
            
            # Persist user turn to MemoManager for session continuity (fast, local)
            if self._memo_manager:
                try:
                    self._memo_manager.append_to_history(self.active, "user", user_text)
                except Exception:
                    logger.debug("Failed to persist user turn to history", exc_info=True)
            
            # Mark that we need a session update (will be done in throttled fashion)
            # Don't call _update_session_context here - it's too slow for the hot path
            # The response_done handler will do a throttled update
            self._pending_session_update = True

            await self._maybe_trigger_call_center_transfer(user_transcript)
            if self._call_center_triggered:
                return

            if self._pending_tool_outputs:
                logger.info("Clinic Recall voice suppressed user-turn create while batched tool outputs are pending")
                return

            # A rapid follow-up during a post-tool response should not create a
            # second normal-turn response. The user item is already in the
            # conversation; replay the completed tool-output response with its
            # deterministic instructions so the model can answer against the
            # latest user turn without racing the in-flight post-tool response.
            if self._post_tool_response_pending:
                if await self._maybe_resume_interrupted_post_tool_response():
                    return
                logger.info("Clinic Recall voice suppressed user-turn create while post-tool response is pending")
                return

            # Single-owner turn-taking. When server VAD `create_response` is disabled
            # (see the agent's turn_detection config), VoiceLive no longer auto-creates
            # the assistant reply for each detected user turn, so the orchestrator must
            # create exactly one response here. This removes the server-vs-orchestrator
            # `response.create` collision that caused both talk-over on barge-in and the
            # "active response" race that silenced the slot-offer turn. When the server
            # still owns creation (other agents), this is a no-op.
            if self._orchestrator_owns_response_create():
                await self._create_user_turn_response()

    async def _handle_transcription_delta(self, event) -> None:
        """Handle user transcription delta."""
        user_transcript = getattr(event, "transcript", "")
        if user_transcript:
            if self._is_clinic_recall_session() and self._is_identity_context():
                logger.debug("[USER delta] Identity-phase transcript suppressed")
                return
            logger.debug("[USER delta] Says: %s", user_transcript)
            # Only update _last_user_message for deltas, don't add to deque yet
            # The final message will be added in _handle_transcription_completed
            self._last_user_message = user_transcript.strip()

    async def _handle_transcription_failed(self, event) -> None:
        """Handle user transcription failure without stranding single-owner turns."""
        logger.info("[USER] Transcription failed; asking caller to repeat")
        if not self._orchestrator_owns_response_create():
            return
        await self._create_user_turn_response(
            additional_instructions="Say exactly: Sorry, I didn't catch that. Could you repeat that?"
        )

    async def _maybe_route_clinic_recall_safety_turn(self, user_text: str) -> bool:
        """Deterministically escalate unsafe Clinic Recall voice turns before model response."""
        if not self._is_clinic_recall_session():
            return False
        if not any(ch.isalnum() for ch in user_text):
            return False

        # Conclusive closings ("that's all, thank you", "no, that's all, bye") are
        # composed only of whitelisted closing/filler fragments, so they are checked
        # before intent classification: live call 2026-07-07 showed "That's all,
        # thank you." classified UNCLEAR→ambiguous, spawning a spurious staff task
        # and leaving the call to die on the idle timeout.
        if self._is_closing_acknowledgement(user_text) and self._is_conclusive_closing(user_text):
            logger.info("Clinic Recall voice conclusive closing; requesting call end | phase=%s", self._call_phase)
            if await self._play_deterministic_line(
                _USER_GOODBYE_LINE,
                speech_key="close-user-goodbye",
                terminal_reason="user_goodbye",
            ):
                # The exact VoiceLive message is the close-out; the transport
                # hangs up after its playout mark. Block the normal model turn.
                self._call_phase = "closing"
                return True
            await self._request_call_end("user_goodbye")
            return False

        intent = classify_intent(user_text)
        if self._is_inbound_clinic_session() and self._is_voice_opt_out_request(user_text):
            args = self._clinic_recall_tool_context()
            result = await execute_tool("record_inbound_opt_out", args)
            recorded = isinstance(result, dict) and result.get("status") == "recorded"
            instruction = (
                _VOICE_OPT_OUT_RECORDED_LINE
                if recorded
                else _VOICE_OPT_OUT_FAILED_LINE
            )
            await self._speak_governed_line(
                instruction,
                speech_key=(
                    "inbound-opt-out-recorded"
                    if recorded
                    else "inbound-opt-out-review"
                ),
                terminal_reason="opt_out",
            )
            self._call_phase = "closing"
            await self._request_call_end("opt_out")
            return True
        if not self._is_inbound_clinic_session() and self._is_voice_opt_out_request(user_text):
            opt_out_status = await self._record_outbound_voice_opt_out()
            if intent not in {InteractionIntent.URGENT, InteractionIntent.CLINICAL}:
                await self._close_voice_after_opt_out(opt_out_status)
                return True
            logger.info(
                "Clinic Recall voice opt-out handled before safety route | status=%s intent=%s",
                opt_out_status,
                intent.value,
            )
        if (
            self._is_inbound_clinic_session()
            and self._awaiting_inbound_availability_confirmation
            and self._is_affirmative_availability_confirmation(user_text)
        ):
            self._awaiting_inbound_availability_confirmation = False
            self._call_phase = "offer"
            logger.info(
                "Inbound Clinic affirmative availability confirmation routed to deterministic tool"
            )
            await self._respond_to_inbound_availability_request()
            return True
        if intent in {InteractionIntent.QUESTION, InteractionIntent.UNCLEAR} and is_conversational_acknowledgement(user_text):
            if await self._maybe_resume_interrupted_post_tool_response():
                return True
            logger.info(
                "Clinic Recall voice allowed benign acknowledgement | intent=%s",
                intent.value,
            )
            return False
        if intent in {InteractionIntent.QUESTION, InteractionIntent.UNCLEAR} and self._is_user_end_request(user_text):
            logger.info("Clinic Recall voice user end request; requesting call end | phase=%s", self._call_phase)
            if await self._play_deterministic_line(
                _USER_GOODBYE_LINE,
                speech_key="close-user-goodbye",
                terminal_reason="user_goodbye",
            ):
                self._call_phase = "closing"
                return True
            await self._request_call_end("user_goodbye")
            return False
        if intent in {InteractionIntent.QUESTION, InteractionIntent.UNCLEAR} and self._is_closing_acknowledgement(user_text):
            logger.info("Clinic Recall voice allowed closing acknowledgement | phase=%s", self._call_phase)
            return False
        if intent in {InteractionIntent.QUESTION, InteractionIntent.UNCLEAR} and self._is_language_preference(user_text):
            logger.info("Clinic Recall voice allowed language preference | phase=%s", self._call_phase)
            return False
        if intent == InteractionIntent.UNCLEAR and self._is_expected_identity_answer(user_text):
            return await self._route_voice_identity_policy_unavailable()
        if (
            self._is_inbound_clinic_session()
            and intent
            in {
                InteractionIntent.REBOOK,
                InteractionIntent.QUESTION,
                InteractionIntent.UNCLEAR,
            }
            and self._is_scheduling_request(user_text)
        ):
            return await self._capture_explicit_inbound_booking_request()
        if (
            self._is_inbound_clinic_session()
            and self._is_explicit_booking_request(user_text, intent)
            and not self._inbound_booking_request_created
            and self._inbound_safety_reason(user_text, intent) is None
        ):
            return await self._capture_explicit_inbound_booking_request()
        if (
            intent in {InteractionIntent.QUESTION, InteractionIntent.UNCLEAR}
            and self._is_inbound_clinic_session()
            and self._is_safe_clinic_logistics_request(user_text)
        ):
            logger.info("Inbound Clinic voice allowed safe clinic logistics question")
            if self._is_clinic_hours_request(user_text):
                await self._respond_to_inbound_clinic_hours()
                return True
            return False
        faq_topic = classify_clinic_faq_topic(user_text)
        if (
            intent in {InteractionIntent.QUESTION, InteractionIntent.UNCLEAR}
            and not self._is_inbound_clinic_session()
            and self._is_safe_recall_faq_request(user_text)
            and faq_topic in SUPPORTED_CLINIC_FAQ_TOPICS
        ):
            logger.info(
                "Clinic Recall voice routing approved FAQ topic | topic=%s",
                faq_topic.value,
            )
            await self._respond_to_recall_clinic_faq(faq_topic)
            return True
        if (
            intent in {InteractionIntent.QUESTION, InteractionIntent.UNCLEAR}
            and self._is_scheduling_context()
            and (self._looks_like_scheduling_answer(user_text) or self._is_scheduling_request(user_text))
        ):
            # A date/time answer during the offer/booking flow is expected; let the
            # model run the deterministic booking tools instead of safety-escalating.
            logger.info("Clinic Recall voice allowed scheduling answer | phase=%s", self._call_phase)
            return False
        if (
            intent in {InteractionIntent.QUESTION, InteractionIntent.UNCLEAR}
            and self._is_inbound_clinic_session()
            and self._is_scheduling_request(user_text)
        ):
            # Live call 2026-07-07 (CAda614): a first-turn availability question
            # ("what are the closest available times you can schedule for me?") has
            # no scheduling context yet and was misrouted to the ambiguous
            # escalation. Explicit scheduling requests on an inbound clinic call are
            # the booking flow — let the model + deterministic booking tools handle
            # them. _is_scheduling_request already fails closed on complaint/negative
            # and distress terms, and clinical/urgent intents never reach this branch.
            logger.info(
                "Inbound Clinic voice allowed first-turn scheduling request | phase=%s",
                self._call_phase,
            )
            return False
        reason_by_intent = {
            InteractionIntent.URGENT: EscalationReason.URGENT.value,
            InteractionIntent.CLINICAL: EscalationReason.CLINICAL.value,
            InteractionIntent.QUESTION: EscalationReason.AMBIGUOUS.value,
            InteractionIntent.UNCLEAR: EscalationReason.AMBIGUOUS.value,
        }
        reason = self._inbound_safety_reason(user_text, intent) or reason_by_intent.get(intent)
        if reason is None:
            return False

        if (
            reason == EscalationReason.AMBIGUOUS.value
            and intent == InteractionIntent.UNCLEAR
            and self._is_inbound_clinic_session()
            and self._is_short_noise_transcript(user_text)
        ):
            self._consecutive_noise_turns += 1
            if self._consecutive_noise_turns == 1:
                # Live call dc64f52c (2026-07-10): ASR noise ("Ewa") escalated
                # as ambiguous, creating a spurious staff task and a confusing
                # spoken follow-up. Ask the caller to repeat ONCE; a second
                # consecutive unintelligible turn still fails closed to the
                # ambiguous escalation below.
                logger.info(
                    "Clinic Recall voice noisy short transcript; asking caller to repeat once"
                )
                await self._speak_governed_line(
                    "Say exactly: Sorry, I didn't catch that. Could you repeat that?",
                    speech_key="clarify-noise",
                )
                return True

        args = self._clinic_recall_tool_context()
        if self._is_inbound_clinic_session():
            args.update(
                {
                    "reason": reason,
                    "summary": f"Inbound voice transcript classified as {reason}; routed to staff without clinical advice.",
                }
            )
            if not all(args.get(key) for key in ("_clinic_id", "_inbound_call_id", "_call_direction")):
                logger.warning(
                    "Inbound Clinic voice safety gate missing trusted context; blocking normal response | reason=%s",
                    reason,
                )
                # Arm the terminal close BEFORE creating the governed line so
                # the transport hard-stop guard covers the whole governed
                # window; while un-armed, caller speech can clear, stale-mark,
                # or cancel the terminal response (live probes 2026-07-10).
                if reason in _TERMINAL_ESCALATION_REASONS:
                    await self._request_call_end(reason)
                await self._send_clinic_recall_safety_response(intent)
                return True

            booking_requested = self._has_booking_request(user_text, intent)
            ack_created = False
            if (
                reason == EscalationReason.CLINICAL.value
                and booking_requested
                and not self._contains_instruction_override(user_text)
            ):
                ack_created = await self._send_clinic_recall_safety_ack()
            result = await self._escalate_inbound_to_staff_once(args, reason)
            escalation_succeeded = bool(result.get("success")) if isinstance(result, dict) else False
            logger.info(
                "Inbound Clinic voice safety routed to staff | intent=%s success=%s reason=%s",
                intent.value,
                result.get("success"),
                reason,
            )
            keep_call_open = False
            self._awaiting_inbound_availability_confirmation = keep_call_open
            if keep_call_open:
                logger.info(
                    "Inbound Clinic clinical+booking captured; keeping call open for graceful close | reason=%s",
                    reason,
                )
            should_end_call = (
                reason in _TERMINAL_ESCALATION_REASONS
                or self._contains_instruction_override(user_text)
                or (
                    reason == EscalationReason.CLINICAL.value
                    and not escalation_succeeded
                )
            )
            if not keep_call_open and should_end_call:
                await self._request_call_end(reason)
            terminal_reason = (
                reason if not keep_call_open and should_end_call else None
            )
            continue_admin = (
                reason == EscalationReason.CLINICAL.value
                and terminal_reason is None
                and not keep_call_open
            )
            if ack_created and self._safety_response_inflight and terminal_reason is None:
                self._pending_safety_final_instruction = self._clinic_recall_safety_instruction(
                    intent,
                    booking_request_created=False,
                    keep_call_open=False,
                    terminal_reason=terminal_reason,
                    escalation_succeeded=escalation_succeeded,
                    continue_admin=continue_admin,
                    booking_request_failed=False,
                )
            else:
                await self._send_clinic_recall_safety_response(
                    intent,
                    booking_request_created=False,
                    keep_call_open=False,
                    terminal_reason=terminal_reason,
                    escalation_succeeded=escalation_succeeded,
                    continue_admin=continue_admin,
                    booking_request_failed=False,
                )
            return True

        args.update(
            {
                "reason": reason,
                "context": f"Voice transcript classified as {intent.value}; routed to staff without clinical advice.",
            }
        )
        if not all(args.get(key) for key in ("_clinic_id", "_patient_id", "_outreach_job_id")):
            logger.warning(
                "Clinic Recall voice safety gate missing trusted context; blocking normal response | intent=%s",
                intent.value,
            )
            # Arm before speaking so the transport hard-stop guard covers the
            # governed window (live probes 2026-07-10).
            if intent in {InteractionIntent.URGENT, InteractionIntent.CLINICAL}:
                await self._request_call_end(reason)
            await self._send_clinic_recall_safety_response(intent)
            return True

        result = await execute_tool("escalate_to_staff", args)
        logger.info(
            "Clinic Recall voice safety routed to staff | intent=%s success=%s reason=%s",
            intent.value,
            result.get("success"),
            reason,
        )
        if intent in {InteractionIntent.URGENT, InteractionIntent.CLINICAL}:
            await self._request_call_end(reason)
        await self._send_clinic_recall_safety_response(intent)
        return True

    def _inbound_safety_reason(self, user_text: str, intent: InteractionIntent) -> str | None:
        if not self._is_inbound_clinic_session():
            return None
        text = self._normalise_live_text(user_text)
        instruction_override = self._contains_instruction_override(text)
        if instruction_override:
            logger.info("Inbound Clinic instruction override routed to safety")
        if any(term in text for term in _SAFEGUARDING_TERMS):
            return "safeguarding"
        if any(term in text for term in _SCHEDULING_DISTRESS_TERMS):
            return "distress"
        has_urgent_content = any(
            re.search(rf"\b{re.escape(term)}\b", text) for term in URGENT_TERMS
        )
        if intent == InteractionIntent.URGENT or has_urgent_content:
            return EscalationReason.URGENT.value
        has_clinical_content = any(
            re.search(rf"\b{re.escape(term)}\b", text) for term in CLINICAL_TERMS
        )
        # Clinical precedence: symptom adjectives ("terrible cough") are clinical
        # descriptions, not complaints. Only an explicit complaint cue turns a
        # clinical turn into a complaint (which stays terminal), and even then
        # the complaint's OBJECT decides: "complain about a cough" is clinical
        # content spoken with the word "complain", not a service complaint.
        if (intent == InteractionIntent.CLINICAL or has_clinical_content) and not self._has_explicit_service_complaint(text):
            return EscalationReason.CLINICAL.value
        if instruction_override:
            return EscalationReason.AMBIGUOUS.value
        if any(term in text for term in _COMPLAINT_OR_NEGATIVE_TERMS):
            return EscalationReason.COMPLAINT.value
        if intent == InteractionIntent.CLINICAL:
            return EscalationReason.CLINICAL.value
        if intent in {InteractionIntent.QUESTION, InteractionIntent.UNCLEAR}:
            return EscalationReason.AMBIGUOUS.value
        return None

    def _has_explicit_service_complaint(self, normalised_text: str) -> bool:
        """True when an explicit complaint cue targets the clinic/service.

        Called on CLINICAL-intent turns only. A complaint cue whose object is
        purely clinical/urgent content ("complain about a cough and headache")
        stays clinical; a service target anywhere in the object keeps the turn
        a terminal complaint. Negated mentions ("I'm not complaining") are not
        complaints. An explicit cue with no parseable object fails closed as a
        complaint.
        """
        text = _NEGATED_COMPLAINT_PATTERN.sub(" ", normalised_text)
        if not any(term in text for term in _EXPLICIT_COMPLAINT_TERMS):
            return False
        match = _COMPLAINT_OBJECT_PATTERN.search(text)
        if match is None:
            return True
        complaint_object = re.split(r"[.!?]", match.group(1), maxsplit=1)[0][:120]
        has_service_target = any(
            re.search(rf"\b{re.escape(term)}\b", complaint_object)
            for term in _SERVICE_COMPLAINT_TARGET_TERMS
        )
        if has_service_target:
            return True
        has_clinical_object = any(
            re.search(rf"\b{re.escape(term)}\b", complaint_object)
            for term in _CLINICAL_COMPLAINT_OBJECT_TERMS
        )
        return not has_clinical_object

    def _is_short_noise_transcript(self, user_text: str) -> bool:
        """True for 1-2 word turns that reached the ambiguous escalation path."""
        return 0 < len(self._normalise_live_text(user_text).split()) <= 2

    def _is_expected_identity_answer(self, user_text: str) -> bool:
        return self._is_identity_context() and self._looks_like_identity_answer(user_text)

    def _is_identity_context(self) -> bool:
        """True when the call is in the identity phase or the last assistant turn asked for identity."""
        if self._call_phase == "identity":
            return True
        previous = self._normalise_live_text(self._last_assistant_message or "")
        return bool(previous and any(cue in previous for cue in _IDENTITY_PROMPT_CUES))

    def _closing_ack_chunks(self, user_text: str) -> list[str]:
        """Split a turn into normalised comma/period-separated fragments for closing checks."""
        text = self._normalise_live_text(user_text)
        if not text or any(term in text for term in _COMPLAINT_OR_NEGATIVE_TERMS):
            return []
        return [chunk.strip(" .,!?") for chunk in re.split(r"[,.;!?]+", text) if chunk.strip(" .,!?")]

    def _is_closing_acknowledgement(self, user_text: str) -> bool:
        """True when the turn consists only of closing phrases (e.g. 'that's all, thank you')."""
        chunks = self._closing_ack_chunks(user_text)
        if not chunks:
            return False
        if not all(chunk in _CLOSING_ACK_PHRASES or chunk in _CLOSING_ACK_FILLER for chunk in chunks):
            return False
        return any(chunk in _CLOSING_ACK_PHRASES for chunk in chunks)

    def _is_conclusive_closing(self, user_text: str) -> bool:
        """True when a closing turn conclusively ends the call ('that's all', 'nothing else')."""
        chunks = self._closing_ack_chunks(user_text)
        return bool(chunks) and any(chunk in _CONCLUSIVE_CLOSING_PHRASES for chunk in chunks)

    def _is_scheduling_context(self) -> bool:
        """True when the call is offering/booking or the last assistant turn asked for a date/time."""
        if self._call_phase in {"offer", "booking"}:
            return True
        previous = self._normalise_live_text(self._last_assistant_message or "")
        return bool(previous and any(cue in previous for cue in _SCHEDULING_PROMPT_CUES))

    def _looks_like_scheduling_answer(self, user_text: str) -> bool:
        """Return true for benign date/time answers (e.g. 'tomorrow', 'the 3rd at 2pm')."""
        text = self._normalise_live_text(user_text)
        if not text or any(term in text for term in _COMPLAINT_OR_NEGATIVE_TERMS):
            return False
        if any(term in text for term in _SCHEDULING_DISTRESS_TERMS):
            return False
        if any(phrase in text for phrase in _SCHEDULING_ANSWER_PHRASES):
            return True
        if set(re.findall(r"[a-z]+", text)) & _SCHEDULING_ANSWER_WORDS:
            return True
        return bool(_SCHEDULING_TIME_PATTERN.search(text))

    def _is_scheduling_request(self, user_text: str) -> bool:
        """Return true for benign availability-search requests in scheduling context."""
        text = self._normalise_live_text(user_text)
        if not text or any(term in text for term in _COMPLAINT_OR_NEGATIVE_TERMS):
            return False
        if any(term in text for term in _SCHEDULING_DISTRESS_TERMS):
            return False
        return any(phrase in text for phrase in _SCHEDULING_REQUEST_PHRASES)

    def _is_safe_clinic_logistics_request(self, user_text: str) -> bool:
        text = self._normalise_live_text(user_text)
        if not text or self._contains_instruction_override(text):
            return False
        if any(term in text for term in _COMPLAINT_OR_NEGATIVE_TERMS):
            return False
        if any(term in text for term in _SCHEDULING_DISTRESS_TERMS):
            return False
        return any(phrase in text for phrase in _SAFE_CLINIC_LOGISTICS_PHRASES)

    def _is_safe_recall_faq_request(self, user_text: str) -> bool:
        text = self._normalise_live_text(user_text)
        if not text or self._contains_instruction_override(text):
            return False
        if any(term in text for term in _COMPLAINT_OR_NEGATIVE_TERMS):
            return False
        return not any(term in text for term in _SCHEDULING_DISTRESS_TERMS)

    def _is_voice_opt_out_request(self, user_text: str) -> bool:
        text = self._normalise_live_text(user_text)
        return any(term in text for term in OPT_OUT_TERMS)

    def _is_clinic_hours_request(self, user_text: str) -> bool:
        text = self._normalise_live_text(user_text)
        return bool(re.search(r"\b(?:open|opening|close|closing|hours)\b", text))

    def _is_affirmative_availability_confirmation(self, user_text: str) -> bool:
        text = " ".join(
            re.sub(r"[^a-z0-9\s]", " ", self._normalise_live_text(user_text)).split()
        )
        return text in {
            "go ahead",
            "ok",
            "okay",
            "please do",
            "sure",
            "yeah",
            "yes",
            "yes please",
            "yep",
        }

    def _is_explicit_booking_request(
        self, user_text: str, intent: InteractionIntent
    ) -> bool:
        if intent != InteractionIntent.REBOOK:
            return False
        text = self._normalise_live_text(user_text)
        if any(
            phrase in text
            for phrase in (
                "do not book",
                "don't book",
                "dont book",
                "not book me",
                "stop booking",
            )
        ):
            return False
        return bool(re.search(r"\b(?:book|booking|rebook|reschedule|schedule)\b", text))

    async def _capture_explicit_inbound_booking_request(self) -> bool:
        args = self._clinic_recall_tool_context()
        args["summary"] = "Inbound caller requested an appointment; staff to confirm details."
        created = self._inbound_booking_request_created
        if not created and all(
            args.get(key) for key in ("_clinic_id", "_inbound_call_id", "_call_direction")
        ):
            result = await execute_tool("create_inbound_booking_request", args)
            created = bool(result.get("success"))
            self._inbound_booking_request_created = created
            logger.info(
                "Inbound Clinic explicit booking request captured | success=%s",
                created,
            )

        if created:
            instruction = (
                "Say exactly: I can't verify identity on this call, so I won't discuss or "
                "record appointment details. The clinic team will follow up. Goodbye."
            )
            speech_key = "identity-booking-handoff"
        else:
            instruction = (
                "Say exactly: I can't verify identity on this call, and I couldn't send a "
                "staff alert just now. Please contact the clinic directly. Goodbye."
            )
            speech_key = "identity-booking-handoff-failed"
        await self._speak_governed_line(
            instruction,
            speech_key=speech_key,
            terminal_reason="identity_policy_unavailable",
        )
        self._call_phase = "closing"
        await self._request_call_end("identity_policy_unavailable")
        return True

    async def _respond_to_inbound_availability_request(self) -> None:
        """Run the scoped availability tool, then phrase only its completed result."""
        self._awaiting_inbound_availability_confirmation = False
        now = datetime.now(UTC)
        args = self._clinic_recall_tool_context()
        args.update(
            {
                "window_start": now.isoformat(),
                "window_end": (now + timedelta(days=14)).isoformat(),
                "limit": 3,
            }
        )
        result = await execute_tool("get_available_slots", args)
        if not isinstance(result, dict):
            result = {"success": False, "error": "availability result was unavailable"}
        slots = result.get("slots") if isinstance(result.get("slots"), list) else []
        logger.info(
            "Inbound Clinic deterministic availability completed | success=%s slots=%d",
            result.get("success") is True,
            len(slots),
        )
        await self._speak_governed_line(
            self._format_inbound_availability_instruction(result),
            speech_key="availability-results",
        )

    async def _respond_to_inbound_clinic_hours(self) -> None:
        result = await execute_tool("get_clinic_hours", self._clinic_recall_tool_context())
        if not isinstance(result, dict):
            result = {"success": False}
        logger.info(
            "Inbound Clinic deterministic hours completed | success=%s",
            result.get("success") is True,
        )
        await self._speak_governed_line(
            self._format_inbound_clinic_hours_instruction(result),
            speech_key="clinic-hours",
        )

    async def _respond_to_recall_clinic_faq(self, topic: ClinicFaqTopic) -> None:
        args = self._clinic_recall_tool_context()
        args["topic"] = topic.value
        result = await execute_tool("get_clinic_faq", args)
        if not isinstance(result, dict):
            result = {}
        await self._speak_governed_line(
            f"Say exactly: {format_sample_clinic_faq_answer(result)}",
            speech_key=f"clinic-faq-{topic.value}",
        )

    async def _record_outbound_voice_opt_out(self) -> str:
        args = self._clinic_recall_tool_context()
        args["channel"] = "call"
        result = await execute_tool("record_opt_out", args)
        recorded = isinstance(result, dict) and result.get("success") is True
        logger.info(
            "Clinic Recall outbound opt-out write completed | success=%s",
            recorded,
        )
        return "recorded" if recorded else "failed"

    async def _route_voice_identity_policy_unavailable(self) -> bool:
        """Keep VoiceLive at T0 when raw-factor isolation cannot be proved."""
        args = self._clinic_recall_tool_context()
        if self._is_inbound_clinic_session():
            if all(
                args.get(key)
                for key in ("_clinic_id", "_inbound_call_id", "_call_direction")
            ):
                await self._escalate_inbound_to_staff_once(
                    {
                        **args,
                        "reason": "identity_policy_unavailable",
                        "summary": "Identity verification requires staff review.",
                    },
                    "identity_policy_unavailable",
                )
        elif all(
            args.get(key)
            for key in ("_clinic_id", "_patient_id", "_outreach_job_id")
        ):
            await execute_tool(
                "escalate_to_staff",
                {
                    **args,
                    "reason": EscalationReason.AMBIGUOUS.value,
                    "context": "Identity verification requires staff review.",
                },
            )
        logger.info("Clinic Recall voice identity turn intercepted at T0")
        self._call_phase = "closing"
        await self._speak_governed_line(
            _VOICE_IDENTITY_POLICY_UNAVAILABLE_LINE,
            speech_key="identity-policy-unavailable",
            terminal_reason="identity_policy_unavailable",
        )
        await self._request_call_end("identity_policy_unavailable")
        return True

    async def _close_voice_after_opt_out(self, status: str) -> None:
        instruction = (
            _VOICE_OPT_OUT_RECORDED_LINE
            if status == "recorded"
            else _VOICE_OPT_OUT_FAILED_LINE
        )
        speech_key = "opt-out-recorded" if status == "recorded" else "opt-out-failed"
        played = await self._play_deterministic_line(
            instruction,
            speech_key=speech_key,
            terminal_reason="opt_out",
        )
        if not played:
            await self._request_call_end("opt_out")

    @staticmethod
    def _format_inbound_clinic_hours_instruction(result: dict[str, Any]) -> str:
        contact_hours = result.get("contact_hours")
        if result.get("success") is not True or not isinstance(contact_hours, dict):
            return (
                "Say exactly: I couldn't retrieve the clinic hours just now, so staff will need "
                "to confirm them. Is there another clinic question I can help with?"
            )
        known_days = (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        )
        normalized = {
            day: str(contact_hours.get(day) or "").strip().replace("-", " to ")
            for day in known_days
            if str(contact_hours.get(day) or "").strip()
        }
        if not normalized:
            return (
                "Say exactly: The clinic hours aren't configured here, so staff will need to "
                "confirm them. Is there another clinic question I can help with?"
            )
        try:
            timezone = ZoneInfo(str(result.get("timezone") or "UTC"))
        except Exception:  # noqa: BLE001 - trusted tool output still fails closed
            timezone = ZoneInfo("UTC")
        today = datetime.now(timezone).strftime("%A").lower()
        if today in normalized:
            return (
                f"Say exactly: The clinic's contact hours today are {normalized[today]}. "
                "Is there another clinic question I can help with?"
            )
        labels = [f"{day.title()} {normalized[day]}" for day in known_days if day in normalized][:3]
        schedule = labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"
        return (
            f"Say exactly: The clinic's contact hours are {schedule}. "
            "Is there another clinic question I can help with?"
        )

    @staticmethod
    def _format_inbound_availability_instruction(result: dict[str, Any]) -> str:
        if result.get("success") is not True:
            return (
                "Say exactly: I couldn't complete the calendar check just now, so clinic staff "
                "will confirm available appointments. Is there another clinic question I can help with?"
            )
        slots = result.get("slots") if isinstance(result.get("slots"), list) else []
        try:
            timezone = ZoneInfo(str(result.get("timezone") or "UTC"))
        except Exception:  # noqa: BLE001 - trusted tool output still fails closed
            timezone = ZoneInfo("UTC")
        labels: list[str] = []
        for slot in slots[:3]:
            if not isinstance(slot, dict):
                continue
            try:
                start = datetime.fromisoformat(str(slot.get("start_at") or "").replace("Z", "+00:00"))
                if start.tzinfo is None or start.utcoffset() is None:
                    continue
                local_start = start.astimezone(timezone)
            except (TypeError, ValueError):
                continue
            labels.append(
                f"{local_start.strftime('%A')} {local_start.day} "
                f"{local_start.strftime('%B')} at {local_start.strftime('%H:%M')}"
            )
        if not labels:
            return (
                "Say exactly: I couldn't find an available appointment in the next two weeks, "
                "so clinic staff will confirm other options. Is there another clinic question I can help with?"
            )
        options = labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])}, and {labels[-1]}"
        return f"Say exactly: I found appointments on {options}. Which time would you prefer?"

    def _is_user_end_request(self, user_text: str) -> bool:
        """Return true when the caller asks to end the call (borrows ART STOP_WORDS)."""
        text = self._normalise_live_text(user_text)
        if not text or any(term in text for term in _COMPLAINT_OR_NEGATIVE_TERMS):
            return False
        return any(stop in text for stop in STOP_WORDS)

    def _is_assistant_sign_off(self, message: str) -> bool:
        """Return true when the assistant has clearly concluded the call."""
        text = self._normalise_live_text(message)
        if not text or "?" in message or any(term in text for term in _COMPLAINT_OR_NEGATIVE_TERMS):
            return False
        if any(cue in text for cue in _ASSISTANT_STILL_SOLICITING_CUES):
            return False
        return any(cue in text for cue in _ASSISTANT_SIGN_OFF_CUES)

    def _is_language_preference(self, user_text: str) -> bool:
        """Return true for benign requests to continue the call in English."""
        text = self._normalise_live_text(user_text)
        if not text or any(term in text for term in _COMPLAINT_OR_NEGATIVE_TERMS):
            return False
        return any(phrase in text for phrase in _LANGUAGE_PREFERENCE_PHRASES)

    def _looks_like_non_english_assistant_turn(self, message: str) -> bool:
        """Detect non-English drift (German/Turkish/Spanish/French) in assistant turns."""
        text = f" {self._normalise_live_text(message)} "
        if not text.strip():
            return False
        if _NON_ENGLISH_CHAR_PATTERN.search(text):
            return True
        return sum(1 for marker in _NON_ENGLISH_ASSISTANT_MARKERS if marker in text) >= 2

    async def _request_call_end(self, reason: str) -> None:
        """Ask the transport to end the call after the final close-out message plays."""
        self._call_phase = "closing"
        request = getattr(self.messenger, "request_call_end", None)
        if request is None:
            return
        try:
            await request(reason=reason)
        except Exception:
            logger.debug("Failed to request transport call end", exc_info=True)
            return
        if self._is_clinic_recall_session() and not self._call_outcome_emitted:
            self._call_outcome_emitted = True
            emit_runtime_event(
                "voice.call.outcome",
                {
                    "status": self._call_outcome_status(reason),
                    "transport": self._transport or "unknown",
                },
            )

    @staticmethod
    def _call_outcome_status(reason: str) -> str:
        if reason == "booking_complete":
            return "booked"
        if reason == "opt_out":
            return "opt_out"
        if reason in _MODEL_TERMINAL_ESCALATION_REASONS:
            return "escalated"
        return "completed"

    async def _maybe_request_end_after_escalation_tool(self, name: str | None, args: dict[str, Any]) -> None:
        """End the call when the model itself escalates a terminal safety concern to staff."""
        if name != "escalate_to_staff":
            return
        reason = str(args.get("reason") or "").strip().lower()
        if reason in _MODEL_TERMINAL_ESCALATION_REASONS:
            logger.info("Clinic Recall voice ending call after model escalation | reason=%s", reason)
            await self._request_call_end(reason)

    async def _maybe_request_end_after_tool_result(self, name: str | None, result: Any) -> None:
        """Honor the ART `end_call: True` tool-result convention to terminate the call."""
        if isinstance(result, dict) and result.get("end_call"):
            logger.info("Clinic Recall voice ending call after tool end_call result | tool=%s", name)
            await self._request_call_end("tool_end_call")

    async def _maybe_request_end_after_booking(
        self,
        name: str | None,
        args: dict[str, Any],
        result: Any,
    ) -> None:
        """Arm a clean closeout after confirmed or truthful pending booking output."""
        if not self._is_clinic_recall_session() or not isinstance(result, dict):
            return
        if name in _BOOKING_TOOLS and result.get("success"):
            provider_confirmed = result.get("provider_confirmed") is True
            has_staff_handoff = bool(result.get("staff_handoff_created"))
            if result.get("queued_for_staff") and not has_staff_handoff:
                return
            if not provider_confirmed:
                self._clinic_recall_booking_succeeded = False
                self._pending_booking_end = True
                self._booking_end_requested = False
                logger.info(
                    "Clinic Recall voice armed call end after pending booking acknowledgement"
                )
                return
            self._clinic_recall_booking_succeeded = True
            return
        if name not in _CONFIRMATION_TOOLS or not self._clinic_recall_booking_succeeded:
            return
        if str(args.get("template") or "") != "booking_confirmation" or not result.get("success"):
            return
        self._clinic_recall_booking_succeeded = False
        self._pending_booking_end = True
        self._booking_end_requested = False
        logger.info("Clinic Recall voice armed call end after booking confirmation | tool=%s", name)

    async def _maybe_request_pending_booking_end(self, trigger: str) -> None:
        """End a completed booking only after the assistant close-out has started."""
        if not self._pending_booking_end or self._booking_end_requested:
            return
        self._pending_booking_end = False
        self._booking_end_requested = True
        logger.info("Clinic Recall voice ending call after booking close-out | trigger=%s", trigger)
        await self._request_call_end("booking_complete")

    def _looks_like_identity_answer(self, user_text: str) -> bool:
        text = self._normalise_live_text(user_text)
        if not text or "?" in user_text or any(term in text for term in _COMPLAINT_OR_NEGATIVE_TERMS):
            return False
        if text.strip(" .,!?") in _AFFIRMATIVE_IDENTITY_ANSWERS:
            return True
        for prefix in _IDENTITY_ANSWER_PREFIXES:
            if text.startswith(prefix):
                candidate = text.removeprefix(prefix).strip(" .,!?")
                return any(char.isalpha() for char in candidate)
        return self._looks_like_bare_identity_name(text)

    def _update_call_phase_from_assistant(self, message: str) -> None:
        """Advance the live dialogue phase from an assistant turn's identity/consent cues."""
        text = self._normalise_live_text(message)
        if not text:
            return
        if any(cue in text for cue in _IDENTITY_PROMPT_CUES):
            self._call_phase = "identity"
        elif any(cue in text for cue in _CONSENT_PROMPT_CUES):
            self._call_phase = "consent"

    def _advance_call_phase_for_tool(self, name: str | None) -> None:
        """Advance the live dialogue phase when a flow-defining tool is called."""
        if name in {"get_availability", "get_available_slots"}:
            self._call_phase = "offer"
        elif name in {"book_slot", "reschedule"}:
            self._call_phase = "booking"

    def _looks_like_bare_identity_name(self, text: str) -> bool:
        candidate = text.strip(" .,!?")
        if not candidate or any(term in candidate for term in _BARE_IDENTITY_REJECT_TERMS):
            return False
        words = candidate.replace("-", " ").replace("'", " ").split()
        if not 1 <= len(words) <= 5:
            return False
        return all(any(char.isalpha() for char in word) for word in words)

    async def _maybe_resume_interrupted_post_tool_response(self) -> bool:
        if not self._post_tool_response_interrupted or not self._post_tool_response_instruction:
            return False
        logger.info("Clinic Recall voice resuming interrupted post-tool response")
        try:
            await self.conn.response.cancel()
        except Exception:
            logger.debug("response.cancel() failed before post-tool response replay", exc_info=True)
        self._active_response_id = None
        try:
            await self.conn.response.create(additional_instructions=self._post_tool_response_instruction)
        except Exception as exc:
            if self._is_active_response_create_error(exc):
                logger.info("Post-tool response replay raced active response; retrying after cancel")
                try:
                    await self.conn.response.cancel()
                except Exception:
                    logger.debug("response.cancel() retry failed before post-tool response replay", exc_info=True)
                self._active_response_id = None
                try:
                    await self.conn.response.create(additional_instructions=self._post_tool_response_instruction)
                except Exception:
                    logger.debug("Failed to replay interrupted post-tool response after retry", exc_info=True)
            else:
                logger.debug("Failed to replay interrupted post-tool response", exc_info=True)
        self._post_tool_response_pending = True
        self._post_tool_response_interrupted = False
        return True

    def _build_post_tool_response_instruction(self) -> str | None:
        if not self._completed_tool_outputs_for_followup:
            return None
        tool_results: list[dict[str, Any]] = []
        for name, output_json in self._completed_tool_outputs_for_followup:
            try:
                result: Any = json.loads(output_json)
            except Exception:
                result = output_json
            tool_results.append({"tool": name, "result": result})
        failed_availability_tool = next(
            (
                item["tool"]
                for item in tool_results
                if item["tool"] in {"get_availability", "get_available_slots"}
                and isinstance(item["result"], dict)
                and item["result"].get("success") is False
            ),
            None,
        )
        availability_recovery = ""
        if failed_availability_tool:
            availability_recovery = (
                f"If {failed_availability_tool} failed, say the calendar check did not complete. "
                "If the failure is about datetime or timezone formatting, "
                f"retry {failed_availability_tool} once "
                "with ISO-8601 datetimes that include a timezone; otherwise ask for another preferred day or time. "
            )
        return (
            "The following tool results have already completed. Use them now to answer the patient. "
            "If get_availability or get_available_slots returned slots, offer the available appointment slots from the completed result. "
            f"{availability_recovery}"
            "Do not say you are still checking availability. Completed tool results: "
            f"{json.dumps(tool_results, ensure_ascii=True)}"
        )

    def _clear_post_tool_response_state(self) -> None:
        # Async completions may already be queued for the NEXT RESPONSE_DONE
        # while the current post-tool response is finishing. Preserve their
        # follow-up data; _pending_tool_outputs is the single source of truth.
        if not self._pending_tool_outputs:
            self._completed_tool_outputs_for_followup = []
        self._post_tool_response_pending = False
        self._post_tool_response_interrupted = False
        self._post_tool_response_instruction = None

    @staticmethod
    def _normalise_live_text(text: str) -> str:
        return " ".join(text.strip().lower().split())

    def _is_clinic_recall_session(self) -> bool:
        if self._system_vars.get("scenario") in {"rebooking", "inbound_clinic"}:
            return True
        if self._system_vars.get("call_direction") == "inbound" and all(
            self._system_vars.get(key) for key in ("clinic_id", "inbound_call_id")
        ):
            return True
        return all(self._system_vars.get(key) for key in ("clinic_id", "patient_id", "outreach_job_id"))

    def _is_inbound_clinic_session(self) -> bool:
        return self._system_vars.get("scenario") == "inbound_clinic" or (
            self._system_vars.get("call_direction") == "inbound"
            and bool(self._system_vars.get("inbound_call_id"))
        )

    def _clinic_recall_tool_context(self) -> dict[str, Any]:
        args: dict[str, Any] = {}
        for key in (
            "clinic_id",
            "patient_id",
            "outreach_job_id",
            "call_direction",
            "inbound_call_id",
            "provider",
            "provider_call_id",
            "called_number_id",
            "called_number",
            "caller_number_hash",
            "identity_evidence_id",
            "identity_session_id",
            "identity_route_id",
        ):
            value = None
            if self._memo_manager:
                value = self._memo_manager.get_value_from_corememory(key)
            if not value:
                value = self._system_vars.get(key)
            if value:
                args[f"_{key}"] = value
        return args

    def _has_booking_request(self, user_text: str, intent: InteractionIntent) -> bool:
        text = self._normalise_live_text(user_text)
        return intent == InteractionIntent.REBOOK or any(term in text for term in _BOOKING_REQUEST_TERMS)

    def _contains_instruction_override(self, user_text: str) -> bool:
        """True when the turn tries to override agent instructions or demand clinical advice."""
        text = self._normalise_live_text(user_text)
        return any(cue in text for cue in _INSTRUCTION_OVERRIDE_CUES)

    async def _escalate_inbound_to_staff_once(
        self,
        args: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        if reason in self._inbound_escalation_reasons_created:
            logger.info(
                "Inbound Clinic safety escalation replay suppressed | reason=%s idempotent=true",
                reason,
            )
            return {"success": True, "idempotent": True}
        result = await execute_tool("escalate_inbound_to_staff", args)
        if isinstance(result, dict) and result.get("success") is True:
            self._inbound_escalation_reasons_created.add(reason)
        return result if isinstance(result, dict) else {"success": False}

    @staticmethod
    def _clinic_recall_safety_instruction(
        intent: InteractionIntent,
        *,
        booking_request_created: bool = False,
        keep_call_open: bool = False,
        terminal_reason: str | None = None,
        escalation_succeeded: bool = True,
        continue_admin: bool = False,
        booking_request_failed: bool = False,
    ) -> str:
        """Build the deterministic safety wording. Extracted so the deferred-final
        path (ack-first turns) and the direct path speak byte-identical lines."""
        instruction = (
            "Say exactly: I can't help with clinical symptoms or medication advice on this call. "
            "I've flagged this for the clinic team to follow up."
        )
        if booking_request_created or keep_call_open or booking_request_failed:
            instruction = (
                "Say exactly: I can't advise on symptoms or discuss appointment details on this call. "
                "I've alerted the clinic team and can still help with general clinic information."
            )
        if intent in {InteractionIntent.QUESTION, InteractionIntent.UNCLEAR}:
            instruction = (
                "Say exactly: I am going to have the clinic team follow up so they can help with that."
            )
        if continue_admin and intent == InteractionIntent.CLINICAL:
            instruction = (
                "Say exactly: I can't advise on symptoms, but I've alerted the clinical team. "
                "I can still help with general clinic information."
            )
        # Truthfulness on write failure (2026-07-10 audit): never claim the
        # clinic team was alerted when the escalation write failed. The caller
        # is told to contact the clinic directly instead.
        if not escalation_succeeded:
            instruction = (
                "Say exactly: I can't help with that on this call, and I couldn't send the clinic "
                "team alert just now. Please contact the clinic directly."
            )
        # Softer terminal close (2026-07-08 live-call feedback): when the gate is
        # about to end the call, finish the conversation properly instead of a
        # bare refusal — deterministic 999 signposting on urgent turns (was
        # prompt-layer only, so it could be skipped), and a warm sign-off on
        # every terminal close. No call-back promises: inbound caller numbers
        # are stored hashed, so a promise to ring back may be untrue for
        # anonymous callers. Terminal behavior itself is unchanged.
        if terminal_reason is not None and not keep_call_open:
            suffix = " The clinic team has been notified. Thanks for calling, and take care."
            if not escalation_succeeded:
                suffix = " Thanks for calling, and take care."
            if terminal_reason == "urgent":
                notified = " The clinic team has been notified." if escalation_succeeded else ""
                suffix = (
                    " If this could be an emergency, please call 999 now or seek urgent care."
                    f"{notified} Take care."
                )
            instruction = instruction.rstrip() + suffix
        return instruction

    def _messenger_deterministic_speech(self):
        """Transport capability probe for non-generative exact-line playback."""
        return getattr(self.messenger, "play_deterministic_speech", None)

    async def _play_deterministic_line(
        self,
        instruction: str,
        *,
        speech_key: str,
        terminal_reason: str | None = None,
    ) -> bool:
        """Submit an exact governed line through the transport's VoiceLive path.

        Live call dc64f52c (2026-07-10) proved `response.create` instructions
        are advisory: the model spoke medication advice over a "Say exactly"
        safety line. Fixed safety/outro wording is therefore supplied as a
        pre-generated assistant message rather than authored by the model.
        Returns False when the capability is absent or playback failed; the
        caller must fail closed — no generative fallback for governed lines on
        capable transports.
        """
        player = self._messenger_deterministic_speech()
        if player is None:
            return False
        spoken = instruction.removeprefix("Say exactly: ").strip()
        if not spoken:
            return False
        try:
            played = bool(
                await player(spoken, speech_key=speech_key, terminal_reason=terminal_reason)
            )
        except Exception:
            logger.error(
                "Deterministic governed line failed | key=%s", speech_key, exc_info=True
            )
            return False
        if not played:
            logger.error("Deterministic governed line not played | key=%s", speech_key)
            return False
        return True

    async def _speak_governed_line(
        self,
        instruction: str,
        *,
        speech_key: str,
        terminal_reason: str | None = None,
    ) -> bool:
        """Speak a fixed governed line through VoiceLive when the transport can,
        otherwise the legacy per-response instruction path. A capable transport
        that fails to play stays silent (fail closed) rather than handing fixed
        wording to the generative model."""
        if self._messenger_deterministic_speech() is not None:
            return await self._play_deterministic_line(
                instruction, speech_key=speech_key, terminal_reason=terminal_reason
            )
        await self._create_user_turn_response(
            additional_instructions=instruction,
            disable_tools=True,
        )
        return True

    async def _send_clinic_recall_safety_ack(self) -> bool:
        """Speak an immediate, outcome-free ack for a mixed clinical+booking turn.

        Latency (plan 2026-07-09): the escalation + booking writes plus the final
        response's model TTFT left ~5 s of dead air on clinical+booking turns. The
        ack describes ACTIVITY only — no booked/confirmed/date wording, no advice,
        no callback promises (inbound numbers are stored hashed) — so it is safe to
        speak before the deterministic writes resolve. Returns True when the ack
        response was created (the outcome line must then be deferred to its
        RESPONSE_DONE instead of racing it into dead air).
        """
        if not self.conn:
            return False
        instruction = (
            "Say exactly: I can't advise on symptoms, but I'm alerting the clinic team now."
        )
        if self._messenger_deterministic_speech() is not None:
            async with self._safety_response_lock:
                if self._safety_response_inflight:
                    logger.info(
                        "Clinic Recall safety ack skipped; safety response already in flight"
                    )
                    return False
                self._pending_safety_final_instruction = None
                self._active_response_id = None
                created = await self._play_deterministic_line(
                    instruction, speech_key="safety-clinical-booking-ack"
                )
                self._safety_response_inflight = created
                self._safety_ack_inflight = created
                if created:
                    logger.info("Clinic Recall safety ack created")
                return created
        response_params = ResponseCreateParams(
            instructions=instruction,
            tool_choice="none",
            tools=[],
        )
        async with self._safety_response_lock:
            if self._safety_response_inflight:
                # A safety response is already speaking; coalesce as the direct
                # path does rather than racing a second cancel+create.
                logger.info(
                    "Clinic Recall safety ack skipped; safety response already in flight"
                )
                return False
            # A new ack starts a new safety turn: drop any stale deferred final
            # from a previous turn so it cannot fire on this ack's RESPONSE_DONE
            # before this turn's writes resolve.
            self._pending_safety_final_instruction = None
            created = False
            try:
                await self.conn.response.cancel()
            except Exception:
                logger.debug("response.cancel() failed before Clinic Recall safety ack", exc_info=True)
            self._active_response_id = None
            try:
                await self.conn.response.create(
                    response=response_params,
                    additional_instructions=instruction,
                )
                created = True
            except Exception as exc:
                if self._is_active_response_create_error(exc):
                    logger.info("Clinic Recall safety ack raced active response; retrying after cancel")
                    try:
                        await self.conn.response.cancel()
                    except Exception:
                        logger.debug("response.cancel() retry failed before Clinic Recall safety ack", exc_info=True)
                    self._active_response_id = None
                    try:
                        await self.conn.response.create(
                            response=response_params,
                            additional_instructions=instruction,
                        )
                        created = True
                    except Exception:
                        logger.warning("Failed to create Clinic Recall safety ack after retry", exc_info=True)
                else:
                    # A failed ack is non-fatal (the final safety line still
                    # speaks directly) but never bury a safety-path failure.
                    logger.warning("Failed to create Clinic Recall safety ack", exc_info=True)
            self._safety_response_inflight = created
            self._safety_ack_inflight = created
            if created:
                # T2d latency anchor: deterministic ack issued while the safety
                # writes run. INFO so ack latency is attributable in AppTraces.
                logger.info("Clinic Recall safety ack created")
            return created

    async def _maybe_send_pending_safety_final_response(self) -> bool:
        """Deliver the deferred outcome line once the ack response completes.

        Called from RESPONSE_DONE (the only point where speaking cannot clip the
        ack). Skipped while the caller is talking — the pending line survives and
        is delivered after the next completed response, mirroring the post-tool
        defer/replay contract.
        """
        instruction = self._pending_safety_final_instruction
        if not instruction or not self.conn:
            return False
        if self._user_speech_active:
            logger.info(
                "Clinic Recall deferred safety final held while caller is speaking"
            )
            return False
        self._pending_safety_final_instruction = None
        if self._messenger_deterministic_speech() is not None:
            async with self._safety_response_lock:
                created = await self._play_deterministic_line(
                    instruction,
                    speech_key="safety-clinical-open",
                )
                self._safety_response_inflight = created
                self._safety_ack_inflight = False
                if created:
                    logger.info("Clinic Recall deferred governed safety final created")
                else:
                    logger.error(
                        "Deferred governed safety final unavailable; failing closed"
                    )
                return created
        response_params = ResponseCreateParams(
            instructions=instruction,
            tool_choice="none",
            tools=[],
        )
        async with self._safety_response_lock:
            created = False
            # The ack response just completed, so there is normally no active
            # response to cancel — cancelling here would emit
            # `response_cancel_not_active` (hard error on the Twilio path).
            try:
                await self.conn.response.create(
                    response=response_params,
                    additional_instructions=instruction,
                )
                created = True
            except Exception as exc:
                if self._is_active_response_create_error(exc):
                    logger.info("Deferred safety final raced active response; retrying after cancel")
                    try:
                        await self.conn.response.cancel()
                    except Exception:
                        logger.debug("response.cancel() retry failed before deferred safety final", exc_info=True)
                    self._active_response_id = None
                    try:
                        await self.conn.response.create(
                            response=response_params,
                            additional_instructions=instruction,
                        )
                        created = True
                    except Exception:
                        logger.warning("Failed to create deferred safety final after retry", exc_info=True)
                else:
                    # Caller would hear the ack but never the outcome line —
                    # safety-critical, never bury at debug level.
                    logger.warning("Failed to create deferred safety final response", exc_info=True)
            self._safety_response_inflight = created
            self._safety_ack_inflight = False
            if created:
                logger.info("Clinic Recall deferred safety final response created")
            return created

    async def _send_clinic_recall_safety_response(
        self,
        intent: InteractionIntent,
        *,
        booking_request_created: bool = False,
        keep_call_open: bool = False,
        terminal_reason: str | None = None,
        escalation_succeeded: bool = True,
        continue_admin: bool = False,
        booking_request_failed: bool = False,
    ) -> None:
        if not self.conn:
            return
        instruction = self._clinic_recall_safety_instruction(
            intent,
            booking_request_created=booking_request_created,
            keep_call_open=keep_call_open,
            terminal_reason=terminal_reason,
            escalation_succeeded=escalation_succeeded,
            continue_admin=continue_admin,
            booking_request_failed=booking_request_failed,
        )
        # A direct safety response supersedes any deferred outcome line: it either
        # carries the same turn's outcome or belongs to a newer escalation.
        self._pending_safety_final_instruction = None
        if self._messenger_deterministic_speech() is not None:
            # Non-generative path: the exact line is supplied to VoiceLive as a
            # pre-generated message. Failure stays silent (fail closed).
            async with self._safety_response_lock:
                self._active_response_id = None
                played = await self._play_deterministic_line(
                    instruction,
                    speech_key=f"safety-{intent.value}-{terminal_reason or ('open' if keep_call_open else 'close')}",
                    terminal_reason=terminal_reason,
                )
                self._safety_response_inflight = False
                self._safety_ack_inflight = False
                if played:
                    # T2d latency anchor (same marker as the generative path).
                    logger.info(
                        "Clinic Recall safety response created | intent=%s", intent.value
                    )
                else:
                    logger.error(
                        "Deterministic safety line unavailable; failing closed without model speech | intent=%s",
                        intent.value,
                    )
            return
        response_params = ResponseCreateParams(
            instructions=instruction,
            tool_choice="none",
            tools=[],
        )
        async with self._safety_response_lock:
            if self._safety_response_inflight and not self._safety_ack_inflight:
                # A safety response is already speaking; a second cancel+create would
                # race it into dead air. The pending spoken response covers this turn.
                logger.info(
                    "Clinic Recall safety response already in flight; coalescing duplicate escalation response"
                )
                return
            created = False
            try:
                await self.conn.response.cancel()
            except Exception:
                logger.debug("response.cancel() failed before Clinic Recall safety response", exc_info=True)
            self._active_response_id = None
            # NOTE: the VoiceLive SDK's response.create() is keyword-only with NO
            # `instructions` parameter (only response/event_id/additional_instructions).
            # Live calls on 2026-07-07 proved `instructions=` raised a client-side
            # TypeError that was swallowed at debug level, so every deterministic
            # safety line died silently (dead air). Use additional_instructions,
            # matching every other orchestrator response path.
            try:
                await self.conn.response.create(
                    response=response_params,
                    additional_instructions=instruction,
                )
                created = True
            except Exception as exc:
                if self._is_active_response_create_error(exc):
                    logger.info("Clinic Recall safety response create raced active response; retrying after cancel")
                    try:
                        await self.conn.response.cancel()
                    except Exception:
                        logger.debug("response.cancel() retry failed before Clinic Recall safety response", exc_info=True)
                    self._active_response_id = None
                    try:
                        await self.conn.response.create(
                            response=response_params,
                            additional_instructions=instruction,
                        )
                        created = True
                    except Exception:
                        logger.warning("Failed to create Clinic Recall safety response after retry", exc_info=True)
                else:
                    # A failed safety response means the caller hears silence on a
                    # safety-critical turn — never bury this at debug level.
                    logger.warning("Failed to create Clinic Recall safety response", exc_info=True)
            # Only a successful create marks the window in flight; a failed create
            # must never block the next escalation from speaking (no dead air).
            self._safety_response_inflight = created
            self._safety_ack_inflight = False
            if created:
                # T2d latency anchor: deterministic safety response issued to the
                # model. INFO so safety-turn latency is attributable in AppTraces.
                logger.info(
                    "Clinic Recall safety response created | intent=%s", intent.value
                )

    @staticmethod
    def _is_active_response_create_error(exc: Exception) -> bool:
        return _ACTIVE_RESPONSE_CREATE_ERROR_TEXT in str(exc).lower()

    def _orchestrator_owns_response_create(self) -> bool:
        """True when the active agent disables server-VAD ``create_response``.

        With ``turn_detection.create_response: false`` VoiceLive stops auto-creating
        a response for every detected user turn, so the orchestrator becomes the single
        owner of ``response.create`` (greeting, safety, post-tool, and normal turns).
        When the agent leaves ``create_response`` unset/true (the default for other
        agents), the server still owns normal-turn creation and this returns False.
        """
        agent = self.agents.get(self.active) if self.agents else None
        if agent is None:
            return False
        ua = getattr(agent, "_agent", agent)
        session = getattr(ua, "session", None)
        if isinstance(session, dict):
            turn_detection = session.get("turn_detection")
        else:
            turn_detection = getattr(session, "turn_detection", None)
        if turn_detection is None:
            return False
        if isinstance(turn_detection, dict):
            create_response = turn_detection.get("create_response")
        else:
            create_response = getattr(turn_detection, "create_response", None)
        if isinstance(create_response, str):
            return create_response.strip().lower() == "false"
        return create_response is False

    async def _create_user_turn_response(
        self,
        *,
        additional_instructions: str | None = None,
        disable_tools: bool = False,
    ) -> None:
        """Create exactly one assistant response for a completed user turn.

        Used only when the orchestrator owns response creation (server-VAD
        ``create_response`` disabled). Creates directly — after a normal end-of-turn
        there is no active response to cancel (a barge-in already cancelled the prior
        one), so we avoid the spurious ``response.cancel()`` that would emit
        ``response_cancel_not_active``. If the create still races a server response,
        cancel once and retry.
        """
        if not self.conn:
            return
        kwargs: dict[str, Any] = {}
        if additional_instructions is not None:
            kwargs["additional_instructions"] = additional_instructions
        if disable_tools:
            kwargs["response"] = ResponseCreateParams(
                instructions=additional_instructions,
                tool_choice="none",
                tools=[],
            )
        try:
            await self.conn.response.create(**kwargs)
            return
        except Exception as exc:
            if not self._is_active_response_create_error(exc):
                logger.debug("Failed to create user-turn response", exc_info=True)
                return
            logger.info("user-turn create raced an active response; retrying after cancel")
            try:
                await self.conn.response.cancel()
            except Exception:
                logger.debug("response.cancel() failed before user-turn retry", exc_info=True)
            self._active_response_id = None
            try:
                await self.conn.response.create(**kwargs)
            except Exception:
                logger.debug("Failed to create user-turn response after retry", exc_info=True)

    async def _create_response_safely(
        self,
        *,
        additional_instructions: str | None = None,
        log_label: str = "response",
    ) -> bool:
        """Create one VoiceLive response, cancelling any in-flight response first.

        VoiceLive rejects a second ``response.create()`` while a response is active
        ("Conversation already has an active response"). On the Twilio path a caller
        barge-in makes the server auto-create a response for the new user turn, which
        then collides with our post-tool "offer slots" response and the offer never
        plays. Cancelling before create lets the deterministic tool-output response win,
        and we retry once if the create still races.
        """
        if not self.conn:
            return False
        kwargs: dict[str, Any] = {}
        if additional_instructions is not None:
            kwargs["additional_instructions"] = additional_instructions
        try:
            await self.conn.response.cancel()
        except Exception:
            logger.debug("response.cancel() failed before %s", log_label, exc_info=True)
        self._active_response_id = None
        try:
            await self.conn.response.create(**kwargs)
            return True
        except Exception as exc:
            if self._is_active_response_create_error(exc):
                logger.info("%s create raced active response; retrying after cancel", log_label)
                try:
                    await self.conn.response.cancel()
                except Exception:
                    logger.debug("response.cancel() retry failed before %s", log_label, exc_info=True)
                self._active_response_id = None
                try:
                    await self.conn.response.create(**kwargs)
                    return True
                except Exception:
                    logger.debug("Failed to create %s after retry", log_label, exc_info=True)
                    return False
            logger.debug("Failed to create %s", log_label, exc_info=True)
            return False

    async def _handle_transcript_delta(self, event) -> None:
        """Handle assistant transcript delta (streaming)."""
        transcript_delta = getattr(event, "delta", "") or getattr(event, "transcript", "")

        # Track LLM TTFT for agent-level token/timing accounting. The canonical
        # TTFT telemetry (the voicelive.llm.ttft histogram + the turn-span event)
        # is emitted by the handler, so we deliberately do NOT create a duplicate
        # 0-duration span here — those previously cluttered the dependencies table.
        ttft_ms = self._metrics.record_first_token() if transcript_delta else None
        if ttft_ms is not None:
            # T3 latency anchor: first model token after speech stopped. INFO so
            # the per-turn latency ledger can be rebuilt from AppTraces.
            logger.info(
                "[Orchestrator] LLM TTFT | turn=%d ttft_ms=%.2f agent=%s",
                self._metrics.turn_count,
                ttft_ms,
                self.active,
            )

        if transcript_delta and self.messenger:
            response_id = self._response_id_from_event(event)
            if response_id:
                self._active_response_id = response_id
            else:
                response_id = self._active_response_id
            try:
                await self.messenger.send_assistant_streaming(
                    transcript_delta,
                    sender=self.active,
                    response_id=response_id,
                )
            except Exception:
                logger.debug("Failed to relay assistant streaming delta", exc_info=True)

    async def _handle_transcript_done(self, event) -> None:
        """Handle assistant transcript complete."""
        full_transcript = getattr(event, "transcript", "")
        if full_transcript:
            logger.info("[%s] Agent: %s", self.active, full_transcript)
            # Track assistant response for history persistence
            self._last_assistant_message = full_transcript
            self._update_call_phase_from_assistant(full_transcript)
            if (
                self._is_clinic_recall_session()
                and not self._english_recovery_requested
                and self._looks_like_non_english_assistant_turn(full_transcript)
            ):
                self._pending_english_recovery = True
                logger.info("Clinic Recall voice detected non-English assistant turn; recovery queued")
            if self._is_clinic_recall_session() and self._pending_booking_end:
                await self._maybe_request_pending_booking_end("assistant_transcript_done")
            elif (
                self._is_clinic_recall_session()
                and not self._booking_end_requested
                and self._is_assistant_sign_off(full_transcript)
            ):
                logger.info("Clinic Recall voice assistant sign-off detected; requesting call end")
                await self._request_call_end("assistant_goodbye")
            
            # Persist assistant turn to MemoManager for session continuity
            if self._memo_manager:
                try:
                    self._memo_manager.append_to_history(self.active, "assistant", full_transcript)
                except Exception:
                    logger.debug("Failed to persist assistant turn to history", exc_info=True)
            
            if self.messenger:
                response_id = self._response_id_from_event(event)
                if not response_id:
                    response_id = self._active_response_id
                try:
                    await self.messenger.send_assistant_message(
                        full_transcript,
                        sender=self.active,
                        response_id=response_id,
                    )
                except Exception:
                    logger.debug(
                        "Failed to relay assistant transcript to session UI", exc_info=True
                    )
                if response_id and response_id == self._active_response_id:
                    self._active_response_id = None
            if self._post_tool_response_pending:
                self._clear_post_tool_response_state()

    async def _handle_response_done(self, event) -> None:
        """Handle response complete.

        CRITICAL: When the model makes multiple tool calls in a single response,
        each tool is executed but we defer response.create() until ALL tools finish.
        This handler flushes pending tool outputs and triggers ONE response.
        """
        logger.debug("Response complete")
        async with self._tool_completion_lock:
            self._response_done_epoch += 1
            response_id = self._response_id_from_event(event)
            if response_id and response_id == self._active_response_id:
                self._active_response_id = None
            # Any completed response closes the safety-response window: the next
            # escalation must speak again rather than be coalesced away.
            self._safety_response_inflight = False
            self._safety_ack_inflight = False

            # Ack-first safety turns: the outcome line was deferred until the ack
            # finished speaking. Deliver it now (skipped while the caller talks; the
            # pending line then rides the next RESPONSE_DONE).
            await self._maybe_send_pending_safety_final_response()

            self._emit_model_metrics(event)

            # Flush pending tool outputs if any and trigger ONE model response
            # This prevents duplicate messages when model makes multiple tool calls
            if self._pending_tool_outputs:
                logger.debug(
                    "[Response Done] Flushing %d pending tool outputs",
                    len(self._pending_tool_outputs),
                )

                # Create all tool output items
                for call_id, output_json in self._pending_tool_outputs:
                    try:
                        output_item = FunctionCallOutputItem(
                            call_id=call_id,
                            output=output_json,
                        )
                        await self.conn.conversation.item.create(item=output_item)
                        logger.debug("Created function_call_output item for call_id=%s", call_id)
                    except Exception:
                        logger.warning(
                            "Failed to create tool output item for call_id=%s", call_id, exc_info=True
                        )

                # Clear pending outputs and consume their follow-up data into a
                # stable replay instruction before another completion can append.
                self._pending_tool_outputs = []
                self._post_tool_response_instruction = self._build_post_tool_response_instruction()
                self._completed_tool_outputs_for_followup = []

                # Update session context with collected information BEFORE response
                await self._update_session_context()

                # Advance turn_id once for all tool calls combined
                if self.messenger:
                    self.messenger.advance_turn_for_tool()

                # Trigger ONE response for all tool outputs
                with tracer.start_as_current_span(
                    "voicelive.response.create_batched",
                    kind=trace.SpanKind.SERVER,
                    attributes=create_service_dependency_attrs(
                        source_service="voicelive_orchestrator",
                        target_service="azure_voicelive",
                        call_connection_id=self.call_connection_id,
                        session_id=(
                            getattr(self.messenger, "session_id", None) if self.messenger else None
                        ),
                    ),
                ):
                    await self._create_response_safely(
                        additional_instructions=self._post_tool_response_instruction,
                        log_label="batched tool response",
                    )
                if self._post_tool_response_instruction:
                    self._post_tool_response_pending = True
                    self._post_tool_response_interrupted = False
                logger.info("[Response Done] Triggered single response for batched tool outputs")

        if self._pending_english_recovery and not self._response_had_tool_calls:
            self._pending_english_recovery = False
            self._english_recovery_requested = True
            logger.info("Clinic Recall voice requesting English recovery response")
            await self._create_user_turn_response(
                additional_instructions=_ENGLISH_RECOVERY_INSTRUCTION
            )

        # Reset the tool calls flag
        self._response_had_tool_calls = False

        # Sync state to MemoManager in background to avoid hot path latency
        self._schedule_background_sync()

        # Schedule throttled session update in background - don't block the hot path
        self._schedule_throttled_session_update()

    # ═══════════════════════════════════════════════════════════════════════════
    # AGENT SWITCHING
    # ═══════════════════════════════════════════════════════════════════════════

    async def _switch_to(self, agent_name: str, system_vars: dict):
        """Switch to a different agent and apply its session configuration."""
        previous_agent = self.active
        agent = self.agents[agent_name]

        # Emit invoke_agent summary span for the outgoing agent
        if previous_agent != agent_name and self._metrics._response_count > 0:
            self._emit_agent_summary_span(previous_agent)

        with tracer.start_as_current_span(
            "voicelive_orchestrator.switch_agent",
            kind=trace.SpanKind.INTERNAL,
            attributes=create_service_handler_attrs(
                service_name="LiveOrchestrator._switch_to",
                call_connection_id=self.call_connection_id,
                session_id=getattr(self.messenger, "session_id", None) if self.messenger else None,
            ),
        ) as switch_span:
            switch_span.set_attribute("voicelive.previous_agent", previous_agent)
            switch_span.set_attribute("voicelive.target_agent", agent_name)

            self._cancel_pending_greeting_tasks()

            system_vars = dict(system_vars or {})
            system_vars.setdefault("previous_agent", previous_agent)
            system_vars.setdefault("active_agent", agent.name)

            is_first_visit = agent_name not in self.visited_agents
            self.visited_agents.add(agent_name)
            switch_span.set_attribute("voicelive.is_first_visit", is_first_visit)

            logger.info(
                "[Agent Switch] %s → %s | Context: %s | First visit: %s",
                previous_agent,
                agent_name,
                system_vars,
                is_first_visit,
            )

            greeting = self._select_pending_greeting(
                agent=agent,
                agent_name=agent_name,
                system_vars=system_vars,
                is_first_visit=is_first_visit,
            )
            if greeting:
                self._pending_greeting = greeting
                self._pending_greeting_agent = agent_name
            else:
                self._pending_greeting = None
                self._pending_greeting_agent = None

            handoff_context = sanitize_handoff_context(system_vars.get("handoff_context"))
            if handoff_context:
                system_vars["handoff_context"] = handoff_context
                for key in (
                    "caller_name",
                    "client_id",
                    "institution_name",
                    "service_type",
                    "case_id",
                    "issue_summary",
                    "details",
                    "handoff_reason",
                    "user_last_utterance",
                ):
                    if key not in system_vars and handoff_context.get(key) is not None:
                        system_vars[key] = handoff_context.get(key)

            # Include slots and tool outputs from MemoManager for context continuity
            if self._memo_manager:
                slots = self._memo_manager.get_context("slots", {})
                if slots:
                    system_vars.setdefault("slots", slots)
                    # Also merge collected info directly for easier template access
                    system_vars.setdefault("collected_information", slots)

                tool_outputs = self._memo_manager.get_context("tool_outputs", {})
                if tool_outputs:
                    system_vars.setdefault("tool_outputs", tool_outputs)

            # Auto-load user profile if client_id is present but session_profile is missing
            await _auto_load_user_context(system_vars)

            self.active = agent_name

            try:
                if self.messenger:
                    try:
                        self.messenger.set_active_agent(agent_name)
                    except AttributeError:
                        logger.debug("Messenger does not support set_active_agent", exc_info=True)

                has_handoff = bool(system_vars.get("handoff_context"))
                switch_span.set_attribute("voicelive.is_handoff", has_handoff)

                # VoiceLive binds the generative model at connect() time; it CANNOT be
                # changed via session.update(). If this agent declares a different
                # voicelive_model than the model bound to the live connection, the override
                # is silently ignored for the rest of the call. Surface that clearly.
                try:
                    target_model = agent._agent.get_model_for_mode("voicelive")
                    target_deployment = getattr(target_model, "deployment_id", None)
                    if (
                        target_deployment
                        and self._model_name
                        and target_deployment != self._model_name
                    ):
                        logger.warning(
                            "[Agent Switch] Agent '%s' requests voicelive_model='%s' but the "
                            "VoiceLive connection is bound to '%s'. VoiceLive cannot change models "
                            "mid-call, so the connection model is used. To honor a per-agent model, "
                            "make this agent the scenario's start agent.",
                            agent_name,
                            target_deployment,
                            self._model_name,
                        )
                        switch_span.set_attribute("voicelive.model_override_ignored", target_deployment)
                except Exception:  # pragma: no cover - defensive
                    logger.debug("Failed to evaluate per-agent model on switch", exc_info=True)

                # For handoffs, clear the last assistant message to prevent the new agent
                # from thinking IT said the old agent's handoff statement (e.g., "I'll connect you
                # to our card specialist"). This prevents the new agent from trying to repeat
                # or complete the handoff.
                if has_handoff:
                    self._last_assistant_message = None
                    logger.debug("[Agent Switch] Cleared last assistant message for handoff")

                # For handoffs, DON'T use the handoff_message as a greeting.
                # The handoff_message is meant for the OLD agent to say ("I'll connect you to...")
                # but by the time we're here, the session has switched to the NEW agent.
                # Instead, let the new agent respond naturally as itself.
                # We'll trigger a response after session update, and the new agent will introduce itself.

                with tracer.start_as_current_span(
                    "voicelive.agent.apply_session",
                    kind=trace.SpanKind.SERVER,
                    attributes=create_service_dependency_attrs(
                        source_service="voicelive_orchestrator",
                        target_service="azure_voicelive",
                        call_connection_id=self.call_connection_id,
                        session_id=(
                            getattr(self.messenger, "session_id", None) if self.messenger else None
                        ),
                    ),
                ) as session_span:
                    session_span.set_attribute("voicelive.agent_name", agent_name)
                    session_id = (
                        getattr(self.messenger, "session_id", None) if self.messenger else None
                    )
                    t_apply = time.perf_counter()
                    await agent.apply_voicelive_session(
                        self.conn,
                        system_vars=system_vars,
                        say=None,
                        session_id=session_id,
                        call_connection_id=self.call_connection_id,
                    )
                    apply_ms = (time.perf_counter() - t_apply) * 1000
                    logger.info(
                        "[VoiceLive Startup] apply_session_ms=%.1f | agent=%s",
                        apply_ms, agent_name,
                    )

                # CRITICAL: Inject conversation history as text items for context retention
                # VoiceLive audio models can "forget" context - explicit text items help
                # This must happen AFTER session update but BEFORE first response
                t_hist = time.perf_counter()
                await self._inject_conversation_history()
                hist_ms = (time.perf_counter() - t_hist) * 1000
                if hist_ms > 5:
                    logger.info(
                        "[VoiceLive Startup] inject_history_ms=%.1f | items=%d",
                        hist_ms, len(self._user_message_history),
                    )

                # Schedule greeting fallback if we have a pending greeting
                # This applies to both handoffs and normal agent switches
                if self._pending_greeting and self._pending_greeting_agent == agent_name:
                    self._schedule_greeting_fallback(agent_name)

                # Reset metrics for the new agent (captures summary of previous)
                self._metrics.reset_for_agent_switch(agent_name)

                switch_span.set_status(trace.StatusCode.OK)
            except Exception as ex:
                switch_span.set_status(trace.StatusCode.ERROR, str(ex))
                switch_span.add_event(
                    "agent_switch.error",
                    {"error.type": type(ex).__name__, "error.message": str(ex)},
                )
                logger.exception("Failed to apply session for agent '%s'", agent_name)
                raise

            logger.info("[Active Agent] %s is now active", self.active)

    # ═══════════════════════════════════════════════════════════════════════════
    # TOOL EXECUTION
    # ═══════════════════════════════════════════════════════════════════════════

    def _is_async_business_tool(self, name: str | None) -> bool:
        if not name or name not in _ASYNC_BUSINESS_TOOL_NAMES:
            return False
        return not self.handoff_service.is_handoff(name) and name not in TRANSFER_TOOL_NAMES

    def _dispatch_async_business_tool(
        self,
        call_id: str | None,
        name: str,
        args_json: str | None,
    ) -> None:
        if not call_id:
            logger.warning("Missing call_id for async business tool | tool=%s", name)
            return
        origin_epoch = self._response_done_epoch
        self._response_had_tool_calls = True
        task = asyncio.create_task(
            self._run_async_business_tool(call_id, name, args_json, origin_epoch),
            name=f"voicelive-tool-{name}-{call_id}",
        )
        self._async_tool_tasks.add(task)
        task.add_done_callback(
            lambda completed, tool=name, tool_call_id=call_id: self._on_async_tool_done(
                completed,
                tool,
                tool_call_id,
            )
        )
        logger.info(
            "[Business Tool] Dispatched async | tool=%s call_id=%s",
            name,
            call_id,
        )

    async def _run_async_business_tool(
        self,
        call_id: str,
        name: str,
        args_json: str | None,
        origin_epoch: int,
    ) -> None:
        try:
            await self._execute_tool_call(
                call_id=call_id,
                name=name,
                args_json=args_json,
                async_origin_epoch=origin_epoch,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Async business tool failed | tool=%s call_id=%s",
                name,
                call_id,
                exc_info=True,
            )
            await self._deliver_async_business_tool_output(
                call_id,
                name,
                json.dumps({"success": False, "error": "tool_execution_failed"}),
                origin_epoch,
            )

    def _on_async_tool_done(
        self,
        task: asyncio.Task,
        name: str,
        call_id: str,
    ) -> None:
        self._async_tool_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.warning(
                "Async business tool task ended unexpectedly | tool=%s call_id=%s",
                name,
                call_id,
                exc_info=True,
            )

    async def _deliver_async_business_tool_output(
        self,
        call_id: str,
        name: str,
        output_json: str,
        origin_epoch: int,
    ) -> None:
        async with self._tool_completion_lock:
            if not self._pending_tool_outputs and not self._post_tool_response_pending:
                self._completed_tool_outputs_for_followup = []
            self._completed_tool_outputs_for_followup.append((name, output_json))

            # The originating model response is still open. Reuse the existing
            # RESPONSE_DONE batch drain so multiple calls still produce one reply.
            if origin_epoch == self._response_done_epoch:
                self._pending_tool_outputs.append((call_id, output_json))
                logger.debug(
                    "[Business Tool] Async output queued for RESPONSE_DONE | call_id=%s pending_count=%d",
                    call_id,
                    len(self._pending_tool_outputs),
                )
                return

            if (
                self._active_response_id
                or self._safety_response_inflight
                or self._handoff_response_pending
                or self._post_tool_response_pending
            ):
                self._pending_tool_outputs.append((call_id, output_json))
                logger.info(
                    "[Business Tool] Async output queued behind active response | tool=%s call_id=%s",
                    name,
                    call_id,
                )
                return

            try:
                output_item = FunctionCallOutputItem(call_id=call_id, output=output_json)
                await self.conn.conversation.item.create(item=output_item)
            except Exception:
                logger.warning(
                    "Failed to create async tool output item | tool=%s call_id=%s",
                    name,
                    call_id,
                    exc_info=True,
                )
                return

            self._post_tool_response_instruction = self._build_post_tool_response_instruction()
            self._completed_tool_outputs_for_followup = []
            await self._update_session_context()
            if self.messenger:
                self.messenger.advance_turn_for_tool()

            if self._user_speech_active:
                self._post_tool_response_pending = True
                self._post_tool_response_interrupted = True
                logger.info(
                    "[Business Tool] Async completion deferred while caller speaks | tool=%s call_id=%s",
                    name,
                    call_id,
                )
                return

            async with self._safety_response_lock:
                await self._create_user_turn_response(
                    additional_instructions=self._post_tool_response_instruction,
                )
            self._post_tool_response_pending = bool(self._post_tool_response_instruction)
            self._post_tool_response_interrupted = False
            logger.info(
                "[Business Tool] Async completion response created | tool=%s call_id=%s",
                name,
                call_id,
            )

    async def _execute_tool_call(
        self,
        call_id: str | None,
        name: str | None,
        args_json: str | None,
        *,
        async_origin_epoch: int | None = None,
    ) -> bool:
        """
        Execute tool call via shared tool registry and send result back to model.

        Returns True if this was a handoff (agent switch), False otherwise.
        """
        if not name or not call_id:
            logger.warning("Missing call_id or name for function call")
            return False

        try:
            args = json.loads(args_json) if args_json else {}
        except Exception:
            logger.warning("Could not parse tool arguments for '%s'; using empty dict", name)
            args = {}
        if self._is_clinic_recall_session():
            args = {
                key: value
                for key, value in args.items()
                if not str(key).startswith("_")
            }

        session_id = getattr(self.messenger, "session_id", None) if self.messenger else None
        with tracer.start_as_current_span(
            f"execute_tool {name}",
            kind=trace.SpanKind.INTERNAL,
            attributes={
                "component": "voicelive",
                # App Insights grouping: ai.session.id=call, ai.user.id=session.
                "ai.session.id": self.call_connection_id or "",
                "ai.user.id": session_id or "",
                SpanAttr.SESSION_ID.value: session_id or "",
                SpanAttr.CALL_CONNECTION_ID.value: self.call_connection_id or "",
                "transport.type": self._transport.upper() if self._transport else "ACS",
                SpanAttr.GENAI_OPERATION_NAME.value: GenAIOperation.EXECUTE_TOOL,
                SpanAttr.GENAI_TOOL_NAME.value: name,
                SpanAttr.GENAI_TOOL_CALL_ID.value: call_id,
                SpanAttr.GENAI_TOOL_TYPE.value: "function",
                SpanAttr.GENAI_PROVIDER_NAME.value: GenAIProvider.AZURE_OPENAI,
                "tool.call_id": call_id,
                "tool.parameters_count": len(args),
                "voicelive.tool_name": name,
                "voicelive.tool_id": call_id,
                "voicelive.agent_name": self.active,
                "voicelive.is_acs": self._transport == "acs",
                "voicelive.args_length": len(args_json) if args_json else 0,
                "voicelive.tool.is_handoff": self.handoff_service.is_handoff(name),
                "voicelive.tool.is_transfer": name in TRANSFER_TOOL_NAMES,
            },
        ) as tool_span:

            if name in TRANSFER_TOOL_NAMES:
                if (
                    self._transport_supports_acs()
                    and (not args.get("call_connection_id"))
                    and self.call_connection_id
                ):
                    args.setdefault("call_connection_id", self.call_connection_id)
                if (
                    self._transport_supports_acs()
                    and (not args.get("call_connection_id"))
                    and self.messenger
                ):
                    fallback_call_id = getattr(self.messenger, "call_id", None)
                    if fallback_call_id:
                        args.setdefault("call_connection_id", fallback_call_id)
                if self.messenger:
                    sess_id = getattr(self.messenger, "session_id", None)
                    if sess_id:
                        args.setdefault("session_id", sess_id)

            # Inject session context into tool args (same pattern as SpeechCascade)
            # This allows tools to use already-loaded session data
            if self._memo_manager:
                session_profile = self._memo_manager.get_value_from_corememory("session_profile")
                if session_profile:
                    args["_session_profile"] = session_profile
                # Always inject _client_id so tools can use the verified value
                # Tools should prefer _client_id over client_id when present
                client_id = self._memo_manager.get_value_from_corememory("client_id")
                if client_id:
                    args["_client_id"] = client_id
                for key in ("clinic_id", "patient_id", "outreach_job_id"):
                    value = self._memo_manager.get_value_from_corememory(key)
                    if value:
                        args[f"_{key}"] = value
            for key, value in self._clinic_recall_tool_context().items():
                args[key] = value

            if self._is_clinic_recall_session():
                logger.info(
                    "Executing Clinic Recall tool: %s | parameters=%d",
                    name,
                    len([key for key in args if not key.startswith("_")]),
                )
            else:
                logger.info("Executing tool: %s with args: %s", name, args)

            notify_status = "success"
            notify_error: str | None = None

            # Use full message history for better handoff context
            last_user_message = (self._last_user_message or "").strip()
            if self.handoff_service.is_handoff(name):
                # Build conversation summary from message history
                if self._user_message_history:
                    # Use last message for immediate context
                    if last_user_message:
                        for field in ("details", "issue_summary", "summary", "topic", "handoff_reason"):
                            if not args.get(field):
                                args[field] = last_user_message
                        args.setdefault("user_last_utterance", last_user_message)
                    
                    # Include full conversation context for richer handoff
                    if len(self._user_message_history) > 1:
                        conversation_context = " | ".join(self._user_message_history)
                        args.setdefault("conversation_summary", conversation_context)
                        logger.debug(
                            "[Handoff] Including %d messages in context",
                            len(self._user_message_history),
                        )
                elif last_user_message:
                    # Fallback to single message
                    for field in ("details", "issue_summary", "summary", "topic", "handoff_reason"):
                        if not args.get(field):
                            args[field] = last_user_message
                    args.setdefault("user_last_utterance", last_user_message)

            MFA_TOOL_NAMES = {"send_mfa_code", "resend_mfa_code"}

            if self.messenger:
                try:
                    public_args = {
                        key: value for key, value in args.items() if not key.startswith("_")
                    }
                    await self.messenger.notify_tool_start(
                        call_id=call_id,
                        name=name,
                        args=public_args,
                    )
                except Exception:
                    logger.debug("Tool start messenger notification failed", exc_info=True)
                if name in MFA_TOOL_NAMES:
                    try:
                        await self.messenger.send_status_update(
                            text="Sending a verification code to your email…",
                            sender=self.active,
                            event_label="mfa_status_update",
                        )
                    except Exception:
                        logger.debug("Failed to emit MFA status update", exc_info=True)

            start_ts = time.perf_counter()
            result: dict[str, Any] = {}

            try:
                # Tool execution runs under the enclosing `execute_tool {name}`
                # span, which already carries the tool name, args, and timing — no
                # separate child span is needed.
                result = await execute_tool(name, args)
            except Exception as exc:
                notify_status = "error"
                notify_error = str(exc)
                tool_span.set_status(trace.StatusCode.ERROR, str(exc))
                tool_span.add_event(
                    "tool.execution_error",
                    {"error.type": type(exc).__name__, "error.message": str(exc)},
                )
                if self.messenger:
                    try:
                        await self.messenger.notify_tool_end(
                            call_id=call_id,
                            name=name,
                            status="error",
                            elapsed_ms=(time.perf_counter() - start_ts) * 1000,
                            error=notify_error,
                        )
                    except Exception:
                        logger.debug("Tool end messenger notification failed", exc_info=True)
                raise

            elapsed_ms = (time.perf_counter() - start_ts) * 1000
            tool_span.set_attribute("execution.duration_ms", elapsed_ms)
            tool_span.set_attribute("voicelive.tool.elapsed_ms", elapsed_ms)

            error_payload: str | None = None
            execution_success = True
            if isinstance(result, dict):
                for key in ("success", "ok", "authenticated"):
                    if key in result and not result[key]:
                        notify_status = "error"
                        execution_success = False
                        break
                if notify_status == "error":
                    err_val = result.get("message") or result.get("error")
                    if err_val:
                        error_payload = str(err_val)

            tool_span.set_attribute("execution.success", execution_success)
            tool_span.set_attribute("result.type", type(result).__name__ if result else "None")
            tool_span.set_attribute("voicelive.tool.status", notify_status)

            # Persist slots and tool outputs from result to MemoManager
            # This ensures collected information is available in subsequent turns
            if isinstance(result, dict) and self._memo_manager:
                try:
                    # Update slots if tool returned any
                    if "slots" in result and isinstance(result["slots"], dict):
                        current_slots = self._memo_manager.get_context("slots", {})
                        current_slots.update(result["slots"])
                        self._memo_manager.set_context("slots", current_slots)
                        self._system_vars["slots"] = current_slots
                        self._system_vars["collected_information"] = current_slots
                        logger.info(
                            "[Tool] Updated slots from %s: %s",
                            name,
                            list(result["slots"].keys()),
                        )

                    # Store tool output for context continuity
                    tool_outputs = self._memo_manager.get_context("tool_outputs", {})
                    # Store a summary of the result, not the full payload
                    output_summary = {
                        k: v
                        for k, v in result.items()
                        if k not in ("slots", "raw_response") and not k.startswith("_")
                    }
                    if output_summary:
                        tool_outputs[name] = output_summary
                        self._memo_manager.set_context("tool_outputs", tool_outputs)
                        self._system_vars["tool_outputs"] = tool_outputs

                    # Persist authenticated identity to corememory so handoff targets
                    # can inject _client_id and render session_profile in their prompts
                    if result.get("authenticated") and result.get("client_id"):
                        cid = result["client_id"]
                        self._memo_manager.set_corememory("client_id", cid)
                        self._system_vars["client_id"] = cid
                        if result.get("caller_name"):
                            self._memo_manager.set_corememory("caller_name", result["caller_name"])
                            self._system_vars["caller_name"] = result["caller_name"]
                        logger.info(
                            "🔐 Persisted authenticated identity to corememory | client_id=%s",
                            cid[:8] + "..." if len(cid) > 8 else cid,
                        )

                    # Persist loaded profile to corememory for cross-agent availability
                    if result.get("success") and result.get("profile") and isinstance(result["profile"], dict):
                        profile = result["profile"]
                        self._memo_manager.set_corememory("session_profile", profile)
                        self._system_vars["session_profile"] = profile
                        if profile.get("client_id"):
                            self._memo_manager.set_corememory("client_id", profile["client_id"])
                            self._system_vars["client_id"] = profile["client_id"]
                        if profile.get("full_name"):
                            self._memo_manager.set_corememory("caller_name", profile["full_name"])
                            self._system_vars["caller_name"] = profile["full_name"]
                        if profile.get("customer_intelligence"):
                            self._memo_manager.set_corememory("customer_intelligence", profile["customer_intelligence"])
                            self._system_vars["customer_intelligence"] = profile["customer_intelligence"]
                        if profile.get("institution_name"):
                            self._memo_manager.set_corememory("institution_name", profile["institution_name"])
                            self._system_vars["institution_name"] = profile["institution_name"]
                        logger.info(
                            "📋 Persisted user profile to corememory | client=%s name=%s",
                            profile.get("client_id", "?")[:8],
                            profile.get("full_name", "?"),
                        )
                except Exception:
                    logger.debug("Failed to persist tool results to MemoManager", exc_info=True)

            # Handle transfer tools
            if (
                name in TRANSFER_TOOL_NAMES
                and notify_status != "error"
                and isinstance(result, dict)
            ):
                takeover_message = result.get("message") or "Transferring call to destination."
                tool_span.add_event(
                    "tool.transfer_initiated",
                    {"transfer.message": takeover_message[:100] if takeover_message else ""},
                )
                if self.messenger:
                    try:
                        await self.messenger.send_status_update(
                            text=takeover_message,
                            sender=self.active,
                            event_label="acs_call_transfer_status",
                        )
                    except Exception:
                        logger.debug("Failed to emit transfer status update", exc_info=True)
                try:
                    if result.get("should_interrupt_playback", True):
                        await self.conn.response.cancel()
                except Exception:
                    logger.debug("response.cancel() failed during transfer", exc_info=True)
                if self.audio:
                    try:
                        await self.audio.stop_playback()
                    except Exception:
                        logger.debug("Audio stop playback failed during transfer", exc_info=True)
                if self.messenger:
                    try:
                        await self.messenger.notify_tool_end(
                            call_id=call_id,
                            name=name,
                            status=notify_status,
                            elapsed_ms=(time.perf_counter() - start_ts) * 1000,
                            result=result,
                            error=error_payload,
                        )
                    except Exception:
                        logger.debug("Tool end messenger notification failed", exc_info=True)
                tool_span.set_status(trace.StatusCode.OK)
                return False

            # Handle handoff tools using unified HandoffService
            if self.handoff_service.is_handoff(name):
                # Use HandoffService for consistent resolution across orchestrators
                resolution = self.handoff_service.resolve_handoff(
                    tool_name=name,
                    tool_args=args,
                    source_agent=self.active,
                    current_system_vars=self._system_vars,
                    user_last_utterance=last_user_message,
                    tool_result=result if isinstance(result, dict) else None,
                )

                if not resolution.success:
                    logger.warning(
                        "Handoff resolution failed: %s | tool=%s",
                        resolution.error,
                        name,
                    )
                    notify_status = "error"
                    tool_span.set_status(trace.StatusCode.ERROR, "handoff_resolution_failed")
                    if self.messenger:
                        try:
                            await self.messenger.notify_tool_end(
                                call_id=call_id,
                                name=name,
                                status=notify_status,
                                elapsed_ms=(time.perf_counter() - start_ts) * 1000,
                                result=result if isinstance(result, dict) else None,
                                error=resolution.error or "handoff_resolution_failed",
                            )
                        except Exception:
                            logger.debug("Tool end messenger notification failed", exc_info=True)
                    return False

                target = resolution.target_agent
                tool_span.set_attribute("voicelive.handoff.target_agent", target)
                tool_span.add_event("tool.handoff_triggered", {"target_agent": target})
                tool_span.set_attribute("voicelive.handoff.share_context", resolution.share_context)
                tool_span.set_attribute("voicelive.handoff.greet_on_switch", resolution.greet_on_switch)
                tool_span.set_attribute("voicelive.handoff.type", resolution.handoff_type)

                # CRITICAL: Cancel any ongoing response from the OLD agent immediately.
                # This prevents the old agent from saying "I'll connect you..." while
                # the session switches to the new agent.
                try:
                    await self.conn.response.cancel()
                    logger.debug("[Handoff] Cancelled old agent response before switch")
                except Exception:
                    pass  # No active response to cancel

                # Stop audio playback to prevent old agent's voice from continuing
                if self.audio:
                    try:
                        await self.audio.stop_playback()
                    except Exception:
                        logger.debug("[Handoff] Audio stop failed", exc_info=True)

                # Use system_vars from HandoffService resolution
                ctx = resolution.system_vars

                logger.info("[Handoff Tool] '%s' triggered | %s → %s", name, self.active, target)

                await self._switch_to(target, ctx)
                self._last_user_message = None

                if result.get("call_center_transfer"):
                    transfer_args: dict[str, Any] = {}
                    if self._transport_supports_acs() and self.call_connection_id:
                        transfer_args["call_connection_id"] = self.call_connection_id
                    if self.messenger:
                        sess_id = getattr(self.messenger, "session_id", None)
                        if sess_id:
                            transfer_args["session_id"] = sess_id
                    if transfer_args:
                        self._call_center_triggered = True
                        await self._trigger_call_center_transfer(transfer_args)
                if self.messenger:
                    try:
                        await self.messenger.notify_tool_end(
                            call_id=call_id,
                            name=name,
                            status=notify_status,
                            elapsed_ms=(time.perf_counter() - start_ts) * 1000,
                            result=result if isinstance(result, dict) else None,
                            error=error_payload,
                        )
                    except Exception:
                        logger.debug("Tool end messenger notification failed", exc_info=True)

                # NOTE: We intentionally do NOT send the handoff tool output back to the model.
                # The old agent's tool call was an internal action that triggered the switch.
                # Sending the output to the new agent's session would confuse it - the new
                # agent would see a tool call it didn't make and might try to "complete" it.
                # Instead, we trigger the new agent's response cleanly via additional_instructions.
                logger.debug(
                    "[Handoff] Skipping tool output injection | "
                    "call_id=%s | The new agent will respond via additional_instructions",
                    call_id,
                )

                # Trigger the new agent to respond naturally as itself
                # Build context about the handoff for the new agent's instruction
                handoff_ctx = ctx.get("handoff_context", {})
                user_question = (
                    handoff_ctx.get("question")
                    or handoff_ctx.get("details")
                    or last_user_message
                    or "general inquiry"
                )
                handoff_summary = (
                    result.get("handoff_summary", "") if isinstance(result, dict) else ""
                )

                # Get handoff mode from context (set by build_handoff_system_vars)
                greet_on_switch = ctx.get("greet_on_switch", True)

                # Trigger the new agent to respond immediately (no background task)
                # The agent's system prompt already contains discrete/announced handoff instructions
                # via is_handoff and greet_on_switch template variables.
                #
                # CRITICAL: Use additional_instructions (which APPENDS to system prompt)
                # instead of ResponseCreateParams(instructions=...) which OVERRIDES it!
                # The agent's prompt template has discrete handoff behavior built in.
                try:
                    # Build additional instruction to append (not override) the system prompt
                    if greet_on_switch:
                        # Announced mode: greeting will be spoken, then address request
                        additional_instruction = (
                            f'The customer\'s request: "{user_question}". '
                            f"Address their request directly after your greeting."
                        )
                        if handoff_summary:
                            additional_instruction += f" Context: {handoff_summary}"
                    else:
                        # Discrete mode: system prompt already has discrete handoff instructions
                        # Just provide the user's question as context - don't override behavior
                        additional_instruction = (
                            f'The customer\'s request: "{user_question}". '
                            f"Respond immediately without any greeting or introduction."
                        )

                        # CRITICAL FIX: For discrete handoffs, inject the user's question as
                        # an explicit conversation item. This gives the model a concrete user
                        # message to respond to, not just additional_instructions context.
                        # Without this, the model may not generate a response because there's
                        # no actual user turn in the conversation to respond to.
                        if user_question and user_question != "general inquiry":
                            try:
                                text_part = InputTextContentPart(text=user_question)
                                user_item = UserMessageItem(content=[text_part])
                                await self.conn.conversation.item.create(item=user_item)
                                logger.debug(
                                    "[Handoff] Injected user question as conversation item: %s",
                                    user_question[:50] if user_question else "none"
                                )
                            except Exception:
                                logger.debug(
                                    "[Handoff] Failed to inject user question item", exc_info=True
                                )

                    # Trigger response synchronously - no fire-and-forget background task
                    # This ensures the handoff response is reliably triggered
                    #
                    # Use conn.response.create() with additional_instructions parameter
                    # This APPENDS to the session's system prompt rather than overriding it
                    #
                    # Advance turn_id to create a new message segment for the new agent
                    # This ensures the handoff response appears as a fresh message
                    if self.messenger:
                        self.messenger.advance_turn_for_tool()

                    # CRITICAL: Clear pending greeting state BEFORE calling response.create()
                    # The _switch_to() method sets _pending_greeting, and when session_ready
                    # event arrives (from session.update()), _handle_session_ready() would try
                    # to trigger another response via trigger_voicelive_response(). This causes
                    # "Conversation already has an active response" error.
                    # We handle the handoff response here with additional_instructions, so we
                    # must prevent the competing greeting mechanism from also triggering.
                    self._cancel_pending_greeting_tasks()
                    self._pending_greeting = None
                    self._pending_greeting_agent = None

                    # CRITICAL: Set flag to prevent _handle_session_updated from cancelling
                    # this response. The SESSION_UPDATED event from session.update() arrives
                    # async and would cancel our handoff response without this guard.
                    self._handoff_response_pending = True

                    with tracer.start_as_current_span(
                        "voicelive.handoff.response_create",
                        kind=trace.SpanKind.SERVER,
                        attributes=create_service_dependency_attrs(
                            source_service="voicelive_orchestrator",
                            target_service="azure_voicelive",
                            call_connection_id=self.call_connection_id,
                            session_id=(
                                getattr(self.messenger, "session_id", None) if self.messenger else None
                            ),
                        ),
                    ):
                        await self.conn.response.create(
                            additional_instructions=additional_instruction
                        )
                    logger.info(
                        "[Handoff] Triggered new agent '%s' | greet=%s | question=%s",
                        target, greet_on_switch, user_question[:50] if user_question else "none"
                    )
                except Exception as e:
                    logger.warning("[Handoff] Failed to trigger response: %s", e)
                    self._handoff_response_pending = False  # Reset flag on failure

                tool_span.set_status(trace.StatusCode.OK)
                return True

            else:
                # Business tool - queue output for batched response at RESPONSE_DONE
                # This prevents duplicate messages when model makes multiple tool calls
                #
                # CRITICAL: Do NOT call response.create() here! The model may have
                # multiple tool calls in a single response. We queue all outputs and
                # trigger ONE response in _handle_response_done().
                output_json = json.dumps(result)
                self._advance_call_phase_for_tool(name)
                await self._maybe_request_end_after_escalation_tool(name, args)
                await self._maybe_request_end_after_tool_result(name, result)
                await self._maybe_request_end_after_booking(name, args, result)
                self._response_had_tool_calls = True
                if async_origin_epoch is None:
                    if not self._pending_tool_outputs and not self._post_tool_response_pending:
                        self._completed_tool_outputs_for_followup = []
                    self._pending_tool_outputs.append((call_id, output_json))
                    self._completed_tool_outputs_for_followup.append((name, output_json))
                    logger.debug(
                        "[Business Tool] Queued output for call_id=%s | pending_count=%d",
                        call_id,
                        len(self._pending_tool_outputs),
                    )
                else:
                    await self._deliver_async_business_tool_output(
                        call_id,
                        name,
                        output_json,
                        async_origin_epoch,
                    )

                if self.messenger:
                    try:
                        await self.messenger.notify_tool_end(
                            call_id=call_id,
                            name=name,
                            status=notify_status,
                            elapsed_ms=(time.perf_counter() - start_ts) * 1000,
                            result=result if isinstance(result, dict) else None,
                            error=error_payload,
                        )
                    except Exception:
                        logger.debug("Tool end messenger notification failed", exc_info=True)
                tool_span.set_status(trace.StatusCode.OK)
                return False

    # ═══════════════════════════════════════════════════════════════════════════
    # GREETING HELPERS
    # ═══════════════════════════════════════════════════════════════════════════

    def _select_pending_greeting(
        self,
        *,
        agent: UnifiedAgent,
        agent_name: str,
        system_vars: dict,
        is_first_visit: bool,
    ) -> str | None:
        """
        Return a contextual greeting the agent should deliver once the session is ready.

        Delegates to HandoffService.select_greeting() for consistent behavior
        across both orchestrators. The HandoffService handles:
        - Priority 1: Explicit greeting override in system_vars
        - Priority 2: Discrete handoff detection (skip greeting)
        - Priority 3: Render agent's greeting/return_greeting template
        """
        # Determine greet_on_switch from system_vars (set by HandoffService.resolve_handoff)
        greet_on_switch = system_vars.get("greet_on_switch", True)

        greeting = self.handoff_service.select_greeting(
            agent=agent,
            is_first_visit=is_first_visit,
            greet_on_switch=greet_on_switch,
            system_vars=system_vars,
        )

        if greeting:
            logger.debug(
                "[Greeting] Selected greeting for %s | first_visit=%s | len=%d",
                agent_name,
                is_first_visit,
                len(greeting),
            )
        else:
            logger.debug(
                "[Greeting] No greeting for %s | first_visit=%s | greet_on_switch=%s",
                agent_name,
                is_first_visit,
                greet_on_switch,
            )

        return greeting

    def _cancel_pending_greeting_tasks(self) -> None:
        if not self._greeting_tasks:
            return
        for task in list(self._greeting_tasks):
            task.cancel()
        self._greeting_tasks.clear()

    def _schedule_greeting_fallback(self, agent_name: str) -> None:
        if not self._pending_greeting or not self._pending_greeting_agent:
            return

        async def _fallback() -> None:
            try:
                await asyncio.sleep(_GREETING_FALLBACK_DELAY_SECONDS)
                if self._pending_greeting and self._pending_greeting_agent == agent_name:
                    logger.debug(
                        "[GreetingFallback] Triggering fallback introduction for %s", agent_name
                    )
                    try:
                        await self.agents[agent_name].trigger_voicelive_response(
                            self.conn,
                            say=self._pending_greeting,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.debug("[GreetingFallback] Failed to deliver greeting", exc_info=True)
                        return
                    self._pending_greeting = None
                    self._pending_greeting_agent = None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[GreetingFallback] Unexpected error in fallback task", exc_info=True)

        task = asyncio.create_task(
            _fallback(),
            name=f"voicelive-greeting-fallback-{agent_name}",
        )
        task.add_done_callback(lambda t: self._greeting_tasks.discard(t))
        self._greeting_tasks.add(task)

    # ═══════════════════════════════════════════════════════════════════════════
    # CALL CENTER TRANSFER
    # ═══════════════════════════════════════════════════════════════════════════

    async def _maybe_trigger_call_center_transfer(self, transcript: str) -> None:
        """Detect trigger phrases and initiate automatic call center transfer."""
        if self._call_center_triggered:
            return

        normalized = transcript.strip().lower()
        if not normalized:
            return

        if not any(phrase in normalized for phrase in CALL_CENTER_TRIGGER_PHRASES):
            return

        self._call_center_triggered = True
        logger.info(
            "[Auto Transfer] Triggering call center transfer due to phrase match: '%s'", transcript
        )

        args: dict[str, Any] = {}
        if self._transport_supports_acs() and self.call_connection_id:
            args["call_connection_id"] = self.call_connection_id
        if self.messenger:
            session_id = getattr(self.messenger, "session_id", None)
            if session_id:
                args["session_id"] = session_id

        await self._trigger_call_center_transfer(args)

    async def _trigger_call_center_transfer(self, args: dict[str, Any]) -> None:
        """Invoke the call center transfer tool and handle playback cleanup."""
        tool_name = "transfer_call_to_call_center"

        if self.messenger:
            try:
                await self.messenger.send_status_update(
                    text="Routing you to a call center representative…",
                    sender=self.active,
                    event_label="acs_call_transfer_status",
                )
            except Exception:
                logger.debug("Failed to emit pre-transfer status update", exc_info=True)

        try:
            result = await execute_tool(tool_name, args)
        except Exception:
            self._call_center_triggered = False
            logger.exception("Automatic call center transfer failed unexpectedly")
            if self.messenger:
                try:
                    await self.messenger.send_status_update(
                        text="We encountered an issue reaching the call center. Staying with the virtual agent for now.",
                        sender=self.active,
                        event_label="acs_call_transfer_status",
                    )
                except Exception:
                    logger.debug("Failed to emit transfer failure status", exc_info=True)
            return

        if not isinstance(result, dict) or not result.get("success"):
            self._call_center_triggered = False
            error_message = None
            if isinstance(result, dict):
                error_message = result.get("message") or result.get("error")
            logger.warning(
                "Automatic call center transfer request was rejected | result=%s", result
            )
            if self.messenger:
                try:
                    await self.messenger.send_status_update(
                        text=error_message
                        or "Unable to reach the call center right now. I'll stay on the line with you.",
                        sender=self.active,
                        event_label="acs_call_transfer_status",
                    )
                except Exception:
                    logger.debug("Failed to emit transfer rejection status", exc_info=True)
            return

        takeover_message = result.get(
            "message", "Routing you to a live call center representative now."
        )

        if self.messenger:
            try:
                await self.messenger.send_status_update(
                    text=takeover_message,
                    sender=self.active,
                    event_label="acs_call_transfer_status",
                )
            except Exception:
                logger.debug("Failed to emit transfer success status", exc_info=True)

        try:
            if result.get("should_interrupt_playback", True):
                await self.conn.response.cancel()
        except Exception:
            logger.debug(
                "response.cancel() failed during automatic call center transfer", exc_info=True
            )

        if self.audio:
            try:
                await self.audio.stop_playback()
            except Exception:
                logger.debug(
                    "Audio stop playback failed during automatic call center transfer",
                    exc_info=True,
                )

    # ═══════════════════════════════════════════════════════════════════════════
    # TELEMETRY HELPERS
    # ═══════════════════════════════════════════════════════════════════════════

    def _emit_agent_summary_span(self, agent_name: str) -> None:
        """Emit an invoke_agent summary span with accumulated token usage."""
        agent = self.agents.get(agent_name)
        if not agent:
            return

        session_id = getattr(self.messenger, "session_id", None) if self.messenger else None
        # Use metrics for duration and token tracking
        agent_duration_ms = self._metrics.duration_ms

        with tracer.start_as_current_span(
            f"invoke_agent {agent_name}",
            kind=trace.SpanKind.INTERNAL,
            attributes={
                "component": "voicelive",
                # App Insights grouping: ai.session.id=call, ai.user.id=session.
                "ai.session.id": self.call_connection_id or "",
                "ai.user.id": session_id or "",
                SpanAttr.SESSION_ID.value: session_id or "",
                SpanAttr.CALL_CONNECTION_ID.value: self.call_connection_id or "",
                SpanAttr.GENAI_OPERATION_NAME.value: GenAIOperation.INVOKE_AGENT,
                SpanAttr.GENAI_PROVIDER_NAME.value: GenAIProvider.AZURE_OPENAI,
                SpanAttr.GENAI_REQUEST_MODEL.value: self._model_name,
                "gen_ai.agent.name": agent_name,
                "gen_ai.agent.description": getattr(
                    agent, "description", f"VoiceLive agent: {agent_name}"
                ),
                SpanAttr.GENAI_USAGE_INPUT_TOKENS.value: self._metrics.input_tokens,
                SpanAttr.GENAI_USAGE_OUTPUT_TOKENS.value: self._metrics.output_tokens,
                "voicelive.agent_name": agent_name,
                "voicelive.response_count": self._metrics._response_count,
                "voicelive.duration_ms": agent_duration_ms,
            },
        ) as agent_span:
            agent_span.add_event(
                "gen_ai.agent.session_complete",
                {
                    "agent": agent_name,
                    "input_tokens": self._metrics.input_tokens,
                    "output_tokens": self._metrics.output_tokens,
                    "response_count": self._metrics._response_count,
                    "duration_ms": agent_duration_ms,
                },
            )
            logger.debug(
                "[Agent Summary] %s complete | tokens=%d/%d responses=%d duration=%.1fms",
                agent_name,
                self._metrics.input_tokens,
                self._metrics.output_tokens,
                self._metrics._response_count,
                agent_duration_ms,
            )

    def _emit_model_metrics(self, event: Any) -> None:
        """Emit GenAI model-level metrics for App Insights Agents blade."""
        response = getattr(event, "response", None)
        if not response:
            return

        response_id = getattr(response, "id", None)

        usage = getattr(response, "usage", None)
        input_tokens = 0
        output_tokens = 0

        if usage:
            input_tokens = getattr(usage, "input_tokens", None) or getattr(
                usage, "prompt_tokens", None
            ) or 0
            output_tokens = getattr(usage, "output_tokens", None) or getattr(
                usage, "completion_tokens", None
            ) or 0

        # Track tokens and response via unified metrics
        self._metrics.add_tokens(input_tokens=input_tokens, output_tokens=output_tokens)
        self._metrics.record_response()

        model = self._model_name
        from utils.operational_metrics import record_genai_usage

        record_genai_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
        )
        status = getattr(response, "status", None)

        # Get TTFT from metrics if available
        turn_duration_ms = self._metrics.current_ttft_ms

        session_id = getattr(self.messenger, "session_id", None) if self.messenger else None
        span_name = model if model else "gpt-4o-realtime"

        with tracer.start_as_current_span(
            span_name,
            kind=trace.SpanKind.CLIENT,
            attributes={
                "component": "voicelive",
                "call.connection.id": self.call_connection_id or "",
                # App Insights grouping: ai.session.id=call, ai.user.id=session.
                "ai.session.id": self.call_connection_id or "",
                SpanAttr.SESSION_ID.value: session_id or "",
                "ai.user.id": session_id or "",
                "transport.type": self._transport.upper() if self._transport else "ACS",
                SpanAttr.GENAI_OPERATION_NAME.value: GenAIOperation.CHAT,
                SpanAttr.GENAI_SYSTEM.value: "openai",
                SpanAttr.GENAI_REQUEST_MODEL.value: model,
                "voicelive.agent_name": self.active,
            },
        ) as model_span:
            model_span.set_attribute(SpanAttr.GENAI_RESPONSE_MODEL.value, model)

            if response_id:
                model_span.set_attribute(SpanAttr.GENAI_RESPONSE_ID.value, response_id)

            if input_tokens is not None:
                model_span.set_attribute(SpanAttr.GENAI_USAGE_INPUT_TOKENS.value, input_tokens)
            if output_tokens is not None:
                model_span.set_attribute(SpanAttr.GENAI_USAGE_OUTPUT_TOKENS.value, output_tokens)

            if turn_duration_ms is not None:
                model_span.set_attribute(
                    SpanAttr.GENAI_CLIENT_OPERATION_DURATION.value, turn_duration_ms
                )

            # Set TTFT if available from metrics
            ttft_ms = self._metrics.current_ttft_ms
            if ttft_ms is not None:
                model_span.set_attribute(SpanAttr.GENAI_SERVER_TIME_TO_FIRST_TOKEN.value, ttft_ms)

            model_span.add_event(
                "gen_ai.response.complete",
                {
                    "response_id": response_id or "",
                    "status": str(status) if status else "",
                    "input_tokens": input_tokens or 0,
                    "output_tokens": output_tokens or 0,
                    "agent": self.active,
                    "turn_number": self._metrics.turn_count,
                },
            )

            logger.debug(
                "[Model Metrics] Response complete | agent=%s model=%s response_id=%s tokens=%s/%s",
                self.active,
                model,
                response_id or "N/A",
                input_tokens or "N/A",
                output_tokens or "N/A",
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # UTILITY HELPERS
    # ═══════════════════════════════════════════════════════════════════════════

    def _transport_supports_acs(self) -> bool:
        return self._transport == "acs"

    @staticmethod
    def _response_id_from_event(event: Any) -> str | None:
        response = getattr(event, "response", None)
        if response and hasattr(response, "id"):
            return response.id
        return getattr(event, "response_id", None)


__all__ = [
    "LiveOrchestrator",
    "TRANSFER_TOOL_NAMES",
    "CALL_CENTER_TRIGGER_PHRASES",
    "register_voicelive_orchestrator",
    "unregister_voicelive_orchestrator",
    "get_voicelive_orchestrator",
    "get_orchestrator_registry_size",
]

