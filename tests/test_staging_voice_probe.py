from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from typing import Any

import pytest
import websockets
from devops.probes import synthesize_probe_audio
from devops.probes.kql_scorecard import (
    FINGERPRINT_FIELDS,
    REQUIRED_FINGERPRINTS,
    STAGING_WORKSPACE_ID,
    build_aggregate_query,
    evaluate_scorecard,
    fetch_aggregate,
)
from devops.probes.probe_core import (
    FRAME_BYTES,
    ProbeSession,
    build_start_frame,
    extract_stream_parameters,
    twilio_signature,
)
from devops.probes.staging_voice_probe import (
    AUDIO_IDLE_GAP_SECONDS,
    GREETING_GAP_SECONDS,
    LINGER_SECONDS,
    SCENARIOS,
    ProbeEnvironment,
    ProbeVerdict,
    Scenario,
    UrgentVariant,
    VerdictReason,
    build_twiml_form,
)
from devops.probes.synthesize_probe_audio import (
    REQUIRED_UTTERANCE_KEYS,
    _probe_voice,
    get_cognitive_services_token,
    governed_echo_text,
    synthesize_catalog,
)


class AdvancingClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += max(0.0, delay)
        await asyncio.sleep(0)


def test_twilio_signature_uses_url_and_sorted_parameters() -> None:
    url = "https://staging.example.test/api/v1/voice/twilio/twiml"
    params = {"To": "+441234567890", "CallToken": "probe-token", "From": "+440000000000"}
    token = "test-auth-token"
    payload = url + "".join(key + params[key] for key in sorted(params))
    expected = base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()

    assert twilio_signature(url, params, token) == expected


def test_every_twiml_parameter_is_copied_to_start_frame() -> None:
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
    <Response><Connect><Stream url="wss://staging.example.test/stream">
      <Parameter name="clinic_id" value="opaque-clinic" />
      <Parameter name="agent_name" value="inbound-clinic" />
      <Parameter name="trusted_context" value="opaque-context" />
    </Stream></Connect></Response>"""

    parameters = extract_stream_parameters(twiml)
    frame = build_start_frame(
        account_sid="AC" + "0" * 32,
        call_sid="CA" + "1" * 32,
        stream_sid="MZ" + "2" * 32,
        custom_parameters=parameters,
    )

    assert frame["start"]["customParameters"] == parameters
    assert set(parameters) == {"clinic_id", "agent_name", "trusted_context"}


@pytest.mark.asyncio
async def test_probe_core_drives_fake_twilio_wss_without_telephony() -> None:
    clock = AdvancingClock()
    observed: dict[str, Any] = {}

    async def fake_twilio(websocket: Any) -> None:
        observed["connected"] = json.loads(await websocket.recv())
        observed["start"] = json.loads(await websocket.recv())
        inbound = json.loads(await websocket.recv())
        observed["inbound_payload"] = base64.b64decode(inbound["media"]["payload"])

        outbound = base64.b64encode(b"\x7f" * (FRAME_BYTES * 2)).decode()
        await websocket.send(
            json.dumps(
                {
                    "event": "media",
                    "streamSid": observed["start"]["streamSid"],
                    "media": {"payload": outbound},
                }
            )
        )
        await websocket.send(
            json.dumps(
                {
                    "event": "mark",
                    "streamSid": observed["start"]["streamSid"],
                    "mark": {"name": "opaque-mark"},
                }
            )
        )
        while True:
            message = json.loads(await websocket.recv())
            if message.get("event") == "mark":
                observed["mark_echo"] = message
                break
        await websocket.send(
            json.dumps({"event": "clear", "streamSid": observed["start"]["streamSid"]})
        )
        await asyncio.sleep(0)
        await websocket.close(code=1000, reason="complete")

    async with websockets.serve(fake_twilio, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as websocket:
            session = ProbeSession(
                websocket,
                account_sid="AC" + "0" * 32,
                call_sid="CA" + "1" * 32,
                stream_sid="MZ" + "2" * 32,
                clock=clock,
            )
            await session.send_start({"agent_name": "inbound-clinic"})
            session.start_background_tasks()
            await session.wait_for_clear(after_count=0, timeout=2)
            await asyncio.wait_for(session.closed.wait(), timeout=2)
            await session.stop_background_tasks()

    assert observed["connected"]["event"] == "connected"
    assert observed["start"]["start"]["customParameters"] == {"agent_name": "inbound-clinic"}
    assert observed["inbound_payload"] == b"\xff" * FRAME_BYTES
    assert observed["mark_echo"]["mark"]["name"] == "opaque-mark"
    assert session.media_frames_received == 1
    assert session.clear_count == 1
    assert session.close_code == 1000
    assert session.mark_playback_delay_ms is not None
    assert session.mark_playback_delay_ms >= 40.0
    assert session.background_task_count == 0
    assert any(delay >= 0.25 for delay in clock.sleeps)
    assert session.event_ledger == [
        "media_received",
        "mark_received",
        "mark_echoed",
        "clear_received",
        "server_closed",
    ]


def test_scenario_registry_is_complete_and_preserves_timing_defaults() -> None:
    assert [definition.number for definition in SCENARIOS.values()] == list(range(1, 13))
    assert set(SCENARIOS) == set(Scenario)
    assert SCENARIOS[Scenario.URGENT_HARD_STOP].variants == tuple(UrgentVariant)
    assert GREETING_GAP_SECONDS >= 1.0
    assert AUDIO_IDLE_GAP_SECONDS >= 1.2
    assert LINGER_SECONDS >= 45.0


def test_opt_out_probe_requires_text_free_urgent_signpost_evidence() -> None:
    assert "urgent_signpost_created" in FINGERPRINT_FIELDS
    assert "urgent_signpost_created" in REQUIRED_FINGERPRINTS["opt_out"]
    assert "opt_out_recorded" not in REQUIRED_FINGERPRINTS["opt_out"]

    query = build_aggregate_query(
        start="2026-07-11T12:00:00+00:00",
        end="2026-07-11T12:05:00+00:00",
    )

    assert "urgent_signpost_created=countif(" in query
    assert 'Message has "intent=urgent"' in query
    assert 'Message has "terminal_reason=urgent"' not in query


def test_phase_zero_mixed_turns_do_not_depend_on_future_semantics() -> None:
    assert SCENARIOS[Scenario.FOUR_TURN_REPLAY].utterance_keys[0] == ("mixed_lexical_booking")
    assert SCENARIOS[Scenario.ORDERED_CONTINUATION].utterance_keys[0] == ("mixed_lexical_booking")
    assert SCENARIOS[Scenario.MIXED_CLINICAL_BOOKING].utterance_keys == ("mixed_clinical_booking",)


def test_probe_environment_requires_runtime_numbers_and_redacts_repr() -> None:
    values = {
        "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
        "TWILIO_AUTH_TOKEN": "secret-token",
        "PROBE_TO_NUMBER": "+441234567890",
        "PROBE_FROM_NUMBER": "+449876543210",
        "PROBE_BASE_URL": "https://staging.example.test",
    }

    environment = ProbeEnvironment.from_environ(values)

    rendered = repr(environment)
    assert values["TWILIO_AUTH_TOKEN"] not in rendered
    assert values["PROBE_TO_NUMBER"] not in rendered
    assert values["PROBE_FROM_NUMBER"] not in rendered

    for required in ("PROBE_TO_NUMBER", "PROBE_FROM_NUMBER", "PROBE_BASE_URL"):
        incomplete = dict(values)
        incomplete.pop(required)
        with pytest.raises(ValueError, match=required):
            ProbeEnvironment.from_environ(incomplete)


def test_realistic_twiml_form_contains_calltoken_without_verdict_identifiers() -> None:
    environment = ProbeEnvironment.from_environ(
        {
            "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
            "TWILIO_AUTH_TOKEN": "secret-token",
            "PROBE_TO_NUMBER": "+441234567890",
            "PROBE_FROM_NUMBER": "+449876543210",
            "PROBE_BASE_URL": "https://staging.example.test",
        }
    )
    form = build_twiml_form(environment, call_sid="CA" + "1" * 32)
    call_token = json.loads(form["CallToken"])

    assert call_token == {
        "parentCallInfoToken": "probe." + "x" * 40,
        "identityHeaderTokens": [],
    }

    verdict = ProbeVerdict(
        scenario=Scenario.GREETING_SMOKE,
        run_uuid="00000000-0000-4000-8000-000000000000",
        passed=True,
        reason=VerdictReason.PASS,
        checks={"twiml_ok": True},
        counts={"media_frames": 1},
        latencies_ms={"greeting_first_audio": 800.0},
        close_code=None,
        started_at="2026-07-11T00:00:00+00:00",
        ended_at="2026-07-11T00:00:01+00:00",
    )
    encoded = verdict.to_json()

    assert "call_sid" not in encoded.lower()
    assert "to_number" not in encoded.lower()
    assert environment.to_number not in encoded
    assert environment.from_number not in encoded


def test_synthetic_utterance_catalog_covers_all_probe_scenarios() -> None:
    scenario_keys = {key for definition in SCENARIOS.values() for key in definition.utterance_keys}

    assert scenario_keys <= REQUIRED_UTTERANCE_KEYS
    assert {
        "urgent",
        "interrupt",
        "mixed_clinical_booking",
        "yes_please",
        "ambiguous_question",
        "ack_perfect",
        "ack_no_problem",
        "ack_okay",
        "opt_out",
        "opt_out_urgent",
        "goodbye",
        "clinic_hours",
        "catarrh_advice_request",
        "governed_echo",
    } <= REQUIRED_UTTERANCE_KEYS


def test_governed_echo_text_is_derived_from_runtime_wording() -> None:
    from apps.artagent.backend.voice.voicelive.orchestrator import LiveOrchestrator
    from src.clinic_recall.enums import InteractionIntent

    instruction = LiveOrchestrator._clinic_recall_safety_instruction(InteractionIntent.QUESTION)

    assert governed_echo_text() == instruction.removeprefix("Say exactly: ").strip()


def test_probe_voice_maps_voicelive_dragon_hd_to_speech_rest() -> None:
    assert _probe_voice({}) == "en-GB-SoniaNeural"


def test_token_acquisition_uses_direct_cognitive_services_resource() -> None:
    observed: dict[str, Any] = {}

    class Completed:
        stdout = "ephemeral-token\n"

    def fake_run(command: list[str], **kwargs: Any) -> Completed:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return Completed()

    assert get_cognitive_services_token(run=fake_run) == "ephemeral-token"
    assert observed["command"] == [
        "az",
        "account",
        "get-access-token",
        "--resource",
        "https://cognitiveservices.azure.com",
        "--query",
        "accessToken",
        "-o",
        "tsv",
    ]
    assert observed["kwargs"] == {
        "check": True,
        "capture_output": True,
        "text": True,
    }


@pytest.mark.asyncio
async def test_synthesis_uses_rest_mulaw_and_returns_memory_only() -> None:
    observed: list[dict[str, Any]] = []
    resource_id = (
        "/subscriptions/00000000-0000-4000-8000-000000000000/"
        "resourceGroups/probe/providers/Microsoft.CognitiveServices/accounts/speech"
    )

    class Response:
        content = b"\x01\x02\x03"

        def raise_for_status(self) -> None:
            return None

    class Client:
        async def post(self, url: str, **kwargs: Any) -> Response:
            observed.append({"url": url, **kwargs})
            return Response()

    result = await synthesize_catalog(
        {"urgent", "governed_echo"},
        environ={
            "AZURE_SPEECH_ENDPOINT": "https://speech.example.test",
            "AZURE_SPEECH_REGION": "uksouth",
            "AZURE_SPEECH_RESOURCE_ID": resource_id,
            "PROBE_SPEECH_VOICE": "en-GB-TestNeural",
        },
        token_getter=lambda: "ephemeral-token",
        client=Client(),
    )

    assert result == {
        "governed_echo": b"\x01\x02\x03",
        "urgent": b"\x01\x02\x03",
    }
    assert len(observed) == 2
    assert {request["url"] for request in observed} == {
        "https://uksouth.tts.speech.microsoft.com/cognitiveservices/v1"
    }
    for request in observed:
        assert request["headers"]["Authorization"] == (f"Bearer aad#{resource_id}#ephemeral-token")
        assert request["headers"]["X-Microsoft-OutputFormat"] == "raw-8khz-8bit-mono-mulaw"
        assert request["headers"]["Content-Type"] == "application/ssml+xml"
        assert isinstance(request["content"], bytes)


def test_synthesis_rejects_untrusted_speech_region_shape() -> None:
    with pytest.raises(
        synthesize_probe_audio.SynthesisError,
        match="speech_region_invalid",
    ):
        synthesize_probe_audio._speech_endpoint(
            {
                "AZURE_SPEECH_ENDPOINT": "https://speech.example.test",
                "AZURE_SPEECH_REGION": "uksouth/other",
            }
        )


def test_kql_query_returns_aggregate_fields_only() -> None:
    query = build_aggregate_query(
        start="2026-07-11T00:00:00+00:00",
        end="2026-07-11T00:01:00+00:00",
    )

    assert "AppTraces" in query
    assert "summarize" in query
    assert "severity_3_or_higher" in query
    assert 'Message has "affirmative availability"' in query
    assert 'Message has "Interruptible call-end cancelled"' in query
    assert 'Message has "deterministic hours completed"' in query
    assert 'Message has "Barge-in clear sent"' in query
    assert 'Message has "instruction override routed to safety"' in query
    assert "mark_playout_received=countif" in query
    assert "rest_termination=countif" in query
    assert "project TimeGenerated, Message" not in query
    assert "take " not in query


def test_kql_fetch_uses_pinned_staging_workspace_without_printing_rows() -> None:
    observed: dict[str, Any] = {}

    class Completed:
        stdout = json.dumps([{"severity_3_or_higher": 0, "prior_playout": 1}])

    def fake_run(command: list[str], **kwargs: Any) -> Completed:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return Completed()

    result = fetch_aggregate(
        workspace=STAGING_WORKSPACE_ID,
        start="2026-07-11T00:00:00+00:00",
        end="2026-07-11T00:01:00+00:00",
        run=fake_run,
    )

    assert result == {"severity_3_or_higher": 0, "prior_playout": 1}
    assert observed["command"][:6] == [
        "az",
        "monitor",
        "log-analytics",
        "query",
        "-w",
        STAGING_WORKSPACE_ID,
    ]
    assert observed["kwargs"] == {
        "check": True,
        "capture_output": True,
        "text": True,
    }


def test_kql_fetch_normalizes_azure_cli_scalar_strings() -> None:
    class Completed:
        stdout = json.dumps(
            [
                {
                    "TableName": "PrimaryResult",
                    "twilio_connected": "1",
                    "severity_3_or_higher": "0",
                    "semantic_p95_ms": "None",
                    "active_tasks_max_per_anchor_kind": "None",
                }
            ]
        )

    result = fetch_aggregate(
        workspace=STAGING_WORKSPACE_ID,
        start="2026-07-11T00:00:00+00:00",
        end="2026-07-11T00:01:00+00:00",
        run=lambda *args, **kwargs: Completed(),
    )

    assert result == {
        "twilio_connected": 1,
        "severity_3_or_higher": 0,
        "semantic_p95_ms": None,
        "active_tasks_max_per_anchor_kind": None,
    }


def test_scorecard_merges_window_and_counts_without_provider_identifiers() -> None:
    verdict = ProbeVerdict(
        scenario=Scenario.ORDERED_CONTINUATION,
        run_uuid="00000000-0000-4000-8000-000000000000",
        passed=True,
        reason=VerdictReason.PASS,
        checks={"twiml_ok": True},
        counts={"media_frames": 10},
        latencies_ms={"greeting_first_audio": 800.0},
        close_code=None,
        started_at="2026-07-11T00:00:00+00:00",
        ended_at="2026-07-11T00:01:00+00:00",
    ).to_dict()
    aggregate = {
        "severity_3_or_higher": 0,
        "prior_playout": 1,
        "assistant_count": 2,
        "clear_count": 0,
        "escalation_count": 1,
        "booking_count": 1,
        "safety_route_count": 1,
        "shadow_disagreement_count": 0,
        "semantic_p95_ms": 500.0,
        "active_tasks_max_per_anchor_kind": 1,
    }

    scorecard = evaluate_scorecard(verdict, aggregate)
    encoded = json.dumps(scorecard, sort_keys=True)

    assert scorecard["passed"] is True
    assert scorecard["fingerprints"]["prior_playout"] is True
    assert scorecard["window"] == {
        "start": verdict["started_at"],
        "end": verdict["ended_at"],
    }
    assert "call_sid" not in encoded.lower()
    assert "provider" not in encoded.lower()

    aggregate["severity_3_or_higher"] = 1
    failed = evaluate_scorecard(verdict, aggregate)
    assert failed["passed"] is False
    assert "severity_3_or_higher" in failed["reason_codes"]


def test_scorecard_requires_mark_and_rest_for_hard_stop() -> None:
    verdict = ProbeVerdict(
        scenario=Scenario.URGENT_HARD_STOP,
        run_uuid="00000000-0000-4000-8000-000000000000",
        passed=True,
        reason=VerdictReason.PASS,
        started_at="2026-07-11T00:00:00+00:00",
        ended_at="2026-07-11T00:01:00+00:00",
    ).to_dict()
    aggregate = {
        "hard_stop_forward_blocked": 1,
        "mark_playout_received": 0,
        "rest_termination": 1,
        "severity_3_or_higher": 0,
    }

    rest_only = evaluate_scorecard(verdict, aggregate)

    assert rest_only["passed"] is False
    assert "missing_fingerprint_mark_rest_termination" in rest_only["reason_codes"]

    aggregate["mark_playout_received"] = 1
    complete = evaluate_scorecard(verdict, aggregate)

    assert complete["passed"] is True
    assert complete["fingerprints"]["mark_rest_termination"] is True


def test_ordered_continuation_allows_caller_turn_clears() -> None:
    verdict = ProbeVerdict(
        scenario=Scenario.ORDERED_CONTINUATION,
        run_uuid="00000000-0000-4000-8000-000000000000",
        passed=True,
        reason=VerdictReason.PASS,
        started_at="2026-07-11T00:00:00+00:00",
        ended_at="2026-07-11T00:01:00+00:00",
    ).to_dict()
    aggregate = {
        "prior_playout": 1,
        "clear_count": 2,
        "severity_3_or_higher": 0,
    }

    scorecard = evaluate_scorecard(verdict, aggregate)

    assert scorecard["passed"] is True
    assert scorecard["fingerprints"]["prior_playout"] is True


def test_governed_echo_allows_clear_before_echo_injection() -> None:
    verdict = ProbeVerdict(
        scenario=Scenario.GOVERNED_ECHO,
        run_uuid="00000000-0000-4000-8000-000000000000",
        passed=True,
        reason=VerdictReason.PASS,
        checks={"governed_echo_no_clear": True},
        started_at="2026-07-11T00:00:00+00:00",
        ended_at="2026-07-11T00:01:00+00:00",
    ).to_dict()
    aggregate = {
        "governed_echo_suppressed": 1,
        "clear_count": 1,
        "severity_3_or_higher": 0,
    }

    scorecard = evaluate_scorecard(verdict, aggregate)

    assert scorecard["passed"] is True
    assert scorecard["fingerprints"]["governed_echo_suppressed"] is True


def test_duplicate_storm_requires_one_booking_and_idempotent_replay() -> None:
    verdict = ProbeVerdict(
        scenario=Scenario.DUPLICATE_STORM,
        run_uuid="00000000-0000-4000-8000-000000000000",
        passed=True,
        reason=VerdictReason.PASS,
        started_at="2026-07-11T00:00:00+00:00",
        ended_at="2026-07-11T00:01:00+00:00",
    ).to_dict()
    aggregate = {
        "idempotent_replay": 1,
        "booking_count": 1,
        "severity_3_or_higher": 0,
    }

    assert evaluate_scorecard(verdict, aggregate)["passed"] is True

    aggregate["booking_count"] = 2
    failed = evaluate_scorecard(verdict, aggregate)

    assert failed["passed"] is False
    assert "duplicate_booking_count_not_one" in failed["reason_codes"]
