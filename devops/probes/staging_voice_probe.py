#!/usr/bin/env python3
"""Run privacy-minimized synthetic Twilio media probes against staging."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets
from defusedxml import ElementTree as ET

if __package__:
    from .probe_core import ProbeSession, RealClock, extract_stream_parameters, twilio_signature
else:
    from probe_core import ProbeSession, RealClock, extract_stream_parameters, twilio_signature


GREETING_GAP_SECONDS = 1.0
AUDIO_IDLE_GAP_SECONDS = 1.2
LINGER_SECONDS = 45.0
_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
_ACCOUNT_SID = re.compile(r"^AC[0-9a-fA-F]{32}$")
_MACHINE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class Scenario(StrEnum):
    GREETING_SMOKE = "greeting_smoke"
    FOUR_TURN_REPLAY = "four_turn_replay"
    INTERRUPTIBLE_CLOSE = "interruptible_close"
    URGENT_HARD_STOP = "urgent_hard_stop"
    ORDERED_CONTINUATION = "ordered_continuation"
    GOVERNED_ECHO = "governed_echo"
    MIXED_CLINICAL_BOOKING = "mixed_clinical_booking"
    ACKNOWLEDGEMENT_BANK = "acknowledgement_bank"
    GENUINE_BARGE_IN = "genuine_barge_in"
    SEMANTIC_FAULT = "semantic_fault"
    DUPLICATE_STORM = "duplicate_storm"
    OPT_OUT = "opt_out"


class UrgentVariant(StrEnum):
    PRE_ARM = "pre_arm"
    POST_CREATED_PRE_CLAIM = "post_created_pre_claim"
    MID_PLAYBACK = "mid_playback"


class VerdictReason(StrEnum):
    PASS = "pass"
    CONFIGURATION_ERROR = "configuration_error"
    SYNTHESIS_FAILED = "synthesis_failed"
    TWIML_REJECTED = "twiml_rejected"
    STREAM_PARAMETERS_MISSING = "stream_parameters_missing"
    WEBSOCKET_FAILED = "websocket_failed"
    SCENARIO_TIMEOUT = "scenario_timeout"
    SCENARIO_ASSERTION_FAILED = "scenario_assertion_failed"
    UNEXPECTED_FAILURE = "unexpected_failure"


@dataclass(frozen=True)
class ScenarioDefinition:
    number: int
    utterance_keys: tuple[str, ...]
    expects_server_close: bool = False
    variants: tuple[UrgentVariant, ...] = ()


SCENARIOS: dict[Scenario, ScenarioDefinition] = {
    Scenario.GREETING_SMOKE: ScenarioDefinition(1, ()),
    Scenario.FOUR_TURN_REPLAY: ScenarioDefinition(
        2,
        (
            "mixed_lexical_booking",
            "yes_please",
            "clinical_followup",
            "availability_request",
        ),
    ),
    Scenario.INTERRUPTIBLE_CLOSE: ScenarioDefinition(3, ("goodbye", "clinic_hours")),
    Scenario.URGENT_HARD_STOP: ScenarioDefinition(
        4,
        ("urgent", "interrupt"),
        expects_server_close=True,
        variants=tuple(UrgentVariant),
    ),
    Scenario.ORDERED_CONTINUATION: ScenarioDefinition(5, ("mixed_lexical_booking", "yes_please")),
    Scenario.GOVERNED_ECHO: ScenarioDefinition(6, ("ambiguous_question", "governed_echo")),
    Scenario.MIXED_CLINICAL_BOOKING: ScenarioDefinition(7, ("mixed_clinical_booking",)),
    Scenario.ACKNOWLEDGEMENT_BANK: ScenarioDefinition(
        8, ("ack_perfect", "ack_no_problem", "ack_okay")
    ),
    Scenario.GENUINE_BARGE_IN: ScenarioDefinition(9, ("ambiguous_question", "genuine_barge_in")),
    Scenario.SEMANTIC_FAULT: ScenarioDefinition(10, ("semantic_fault",)),
    Scenario.DUPLICATE_STORM: ScenarioDefinition(
        11,
        (
            "duplicate_booking",
            "duplicate_booking",
            "duplicate_clinical",
            "duplicate_clinical",
        ),
    ),
    Scenario.OPT_OUT: ScenarioDefinition(12, ("opt_out_urgent",), expects_server_close=True),
}


@dataclass(frozen=True, repr=False)
class ProbeEnvironment:
    account_sid: str
    auth_token: str
    to_number: str
    from_number: str
    base_url: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> ProbeEnvironment:
        required = (
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "PROBE_TO_NUMBER",
            "PROBE_FROM_NUMBER",
            "PROBE_BASE_URL",
        )
        values: dict[str, str] = {}
        for name in required:
            value = environ.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            values[name] = value
        if not _ACCOUNT_SID.fullmatch(values["TWILIO_ACCOUNT_SID"]):
            raise ValueError("TWILIO_ACCOUNT_SID is malformed")
        for name in ("PROBE_TO_NUMBER", "PROBE_FROM_NUMBER"):
            if not _E164.fullmatch(values[name]):
                raise ValueError(f"{name} must be E.164")
        base_url = values["PROBE_BASE_URL"].rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("PROBE_BASE_URL must be an HTTPS origin")
        return cls(
            account_sid=values["TWILIO_ACCOUNT_SID"],
            auth_token=values["TWILIO_AUTH_TOKEN"],
            to_number=values["PROBE_TO_NUMBER"],
            from_number=values["PROBE_FROM_NUMBER"],
            base_url=base_url,
        )

    @property
    def twiml_url(self) -> str:
        return f"{self.base_url}/api/v1/voice/twilio/twiml"

    @property
    def websocket_url(self) -> str:
        return f"wss://{urlparse(self.base_url).netloc}/api/v1/twilio/stream"

    def __repr__(self) -> str:
        return f"ProbeEnvironment(base_url={self.base_url!r}, credentials='configured')"


@dataclass(frozen=True)
class ProbeVerdict:
    scenario: Scenario
    run_uuid: str
    passed: bool
    reason: VerdictReason
    checks: Mapping[str, bool] = field(default_factory=dict)
    counts: Mapping[str, int] = field(default_factory=dict)
    latencies_ms: Mapping[str, float | None] = field(default_factory=dict)
    close_code: int | None = None
    started_at: str = ""
    ended_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        for values in (self.checks, self.counts, self.latencies_ms):
            if any(not _MACHINE_KEY.fullmatch(key) for key in values):
                raise ValueError("verdict maps require bounded enum-style keys")
        value = asdict(self)
        value["scenario"] = self.scenario.value
        value["reason"] = self.reason.value
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def build_twiml_form(environment: ProbeEnvironment, *, call_sid: str) -> dict[str, str]:
    return {
        "CallSid": call_sid,
        "AccountSid": environment.account_sid,
        "From": environment.from_number,
        "To": environment.to_number,
        "CallStatus": "ringing",
        "Direction": "inbound",
        "ApiVersion": "2010-04-01",
        "CallToken": json.dumps(
            {
                "parentCallInfoToken": "probe." + "x" * 40,
                "identityHeaderTokens": [],
            },
            separators=(",", ":"),
        ),
    }


def _stream_url(twiml: str, fallback: str) -> str:
    root = ET.fromstring(twiml)
    for element in root.iter("Stream"):
        value = element.attrib.get("url")
        if value:
            return value
    return fallback


async def _speak_and_wait(
    session: ProbeSession,
    audio: bytes,
    *,
    expect_response: bool = True,
) -> None:
    before = session.media_frames_received
    done = session.speak(audio)
    await asyncio.wait_for(done.wait(), timeout=15.0)
    if expect_response:
        await session.wait_for_audio_idle(
            after_count=before,
            gap_seconds=AUDIO_IDLE_GAP_SECONDS,
            timeout=40.0,
        )


async def _execute_scenario(
    session: ProbeSession,
    *,
    scenario: Scenario,
    variant: UrgentVariant,
    audio: Mapping[str, bytes],
) -> dict[str, bool]:
    checks: dict[str, bool] = {"greeting_audio": session.media_frames_received > 0}
    if scenario is Scenario.GREETING_SMOKE:
        return checks
    if scenario is Scenario.INTERRUPTIBLE_CLOSE:
        before = session.media_frames_received
        done = session.speak(audio["goodbye"])
        await asyncio.wait_for(done.wait(), timeout=15.0)
        await session.wait_for_new_audio(after_count=before, timeout=30.0)
        await session.clock.sleep(0.25)
        clears_before = session.clear_count
        done = session.speak(audio["clinic_hours"])
        await asyncio.wait_for(done.wait(), timeout=15.0)
        await session.wait_for_clear(after_count=clears_before, timeout=10.0)
        after_clear = session.media_frames_received
        await session.wait_for_audio_idle(
            after_count=after_clear,
            gap_seconds=AUDIO_IDLE_GAP_SECONDS,
            timeout=40.0,
        )
        checks["close_cancelled"] = session.clear_count > clears_before
        checks["call_remained_open"] = not session.closed.is_set()
        return checks
    if scenario is Scenario.URGENT_HARD_STOP:
        done = session.speak(audio["urgent"])
        await asyncio.wait_for(done.wait(), timeout=15.0)
        if variant is UrgentVariant.PRE_ARM:
            await session.clock.sleep(0.7)
        elif variant is UrgentVariant.POST_CREATED_PRE_CLAIM:
            await session.clock.sleep(0.1)
        else:
            before = session.media_frames_received
            await session.wait_for_new_audio(after_count=before, timeout=30.0)
            await session.clock.sleep(1.0)
        session.speak(audio["interrupt"])
        await asyncio.wait_for(session.closed.wait(), timeout=LINGER_SECONDS)
        checks["server_closed"] = session.closed.is_set()
        checks["mark_echoed"] = "mark_echoed" in session.event_ledger
        return checks
    if scenario is Scenario.GOVERNED_ECHO:
        before = session.media_frames_received
        done = session.speak(audio["ambiguous_question"])
        await asyncio.wait_for(done.wait(), timeout=15.0)
        await session.wait_for_new_audio(after_count=before, timeout=30.0)
        clears_before = session.clear_count
        await session.clock.sleep(0.6)
        done = session.speak(audio["governed_echo"])
        await asyncio.wait_for(done.wait(), timeout=15.0)
        await session.clock.sleep(4.0)
        checks["governed_echo_no_clear"] = session.clear_count == clears_before
        return checks
    if scenario is Scenario.GENUINE_BARGE_IN:
        before = session.media_frames_received
        done = session.speak(audio["ambiguous_question"])
        await asyncio.wait_for(done.wait(), timeout=15.0)
        await session.wait_for_new_audio(after_count=before, timeout=30.0)
        clears_before = session.clear_count
        await session.clock.sleep(0.1)
        await _speak_and_wait(session, audio["genuine_barge_in"])
        checks["genuine_barge_clear"] = session.clear_count > clears_before
        return checks

    definition = SCENARIOS[scenario]
    for utterance_key in definition.utterance_keys:
        await _speak_and_wait(
            session,
            audio[utterance_key],
            expect_response=not definition.expects_server_close,
        )
    if definition.expects_server_close:
        await asyncio.wait_for(session.closed.wait(), timeout=LINGER_SECONDS)
        checks["server_closed"] = session.closed.is_set()
    else:
        checks["call_remained_open"] = not session.closed.is_set()
    return checks


async def run_probe(
    environment: ProbeEnvironment,
    *,
    scenario: Scenario,
    urgent_variant: UrgentVariant,
) -> ProbeVerdict:
    run_uuid = str(uuid.uuid4())
    internal_call_sid = "CA" + uuid.uuid4().hex
    internal_stream_sid = "MZ" + uuid.uuid4().hex
    started_wall = datetime.now(UTC)
    clock = RealClock()
    connected_at: float | None = None
    session: ProbeSession | None = None
    twiml_ok = False
    parameters_present = False
    reason = VerdictReason.UNEXPECTED_FAILURE
    checks: dict[str, bool] = {}

    try:
        if __package__:
            from .synthesize_probe_audio import synthesize_catalog
        else:
            from synthesize_probe_audio import synthesize_catalog

        definition = SCENARIOS[scenario]
        audio = await synthesize_catalog(set(definition.utterance_keys))
    except Exception:
        return _failed_verdict(scenario, run_uuid, started_wall, VerdictReason.SYNTHESIS_FAILED)

    form = build_twiml_form(environment, call_sid=internal_call_sid)
    headers = {
        "X-Twilio-Signature": twilio_signature(environment.twiml_url, form, environment.auth_token),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(environment.twiml_url, data=form, headers=headers)
        twiml_ok = (
            response.status_code == 200
            and "<Connect>" in response.text
            and "<Stream" in response.text
        )
        if not twiml_ok:
            reason = VerdictReason.TWIML_REJECTED
            raise RuntimeError
        parameters = extract_stream_parameters(response.text)
        parameters_present = bool(parameters)
        if not parameters_present:
            reason = VerdictReason.STREAM_PARAMETERS_MISSING
            raise RuntimeError
        websocket_url = _stream_url(response.text, environment.websocket_url)
        async with websockets.connect(websocket_url, open_timeout=20.0) as websocket:
            connected_at = clock.monotonic()
            session = ProbeSession(
                websocket,
                account_sid=environment.account_sid,
                call_sid=internal_call_sid,
                stream_sid=internal_stream_sid,
                clock=clock,
            )
            await session.send_start(parameters)
            session.start_background_tasks()
            await session.wait_for_audio_idle(
                after_count=0,
                gap_seconds=GREETING_GAP_SECONDS,
                timeout=30.0,
            )
            checks = await _execute_scenario(
                session,
                scenario=scenario,
                variant=urgent_variant,
                audio=audio,
            )
            if not all(checks.values()):
                reason = VerdictReason.SCENARIO_ASSERTION_FAILED
                raise RuntimeError
        reason = VerdictReason.PASS
    except TimeoutError:
        reason = VerdictReason.SCENARIO_TIMEOUT
    except (httpx.HTTPError, websockets.WebSocketException):
        reason = VerdictReason.WEBSOCKET_FAILED
    except (RuntimeError, ValueError, ET.ParseError):
        if reason is VerdictReason.UNEXPECTED_FAILURE:
            reason = VerdictReason.SCENARIO_ASSERTION_FAILED
    except Exception:
        reason = VerdictReason.UNEXPECTED_FAILURE
    finally:
        if session is not None:
            await session.stop_background_tasks()

    ended_wall = datetime.now(UTC)
    greeting_latency = None
    if session is not None and connected_at is not None and session.first_media_at is not None:
        greeting_latency = round((session.first_media_at - connected_at) * 1000.0, 1)
    return ProbeVerdict(
        scenario=scenario,
        run_uuid=run_uuid,
        passed=reason is VerdictReason.PASS,
        reason=reason,
        checks={
            "twiml_ok": twiml_ok,
            "stream_parameters_present": parameters_present,
            **checks,
        },
        counts={
            "media_frames": session.media_frames_received if session else 0,
            "clears": session.clear_count if session else 0,
            "marks_echoed": session.event_ledger.count("mark_echoed") if session else 0,
        },
        latencies_ms={
            "greeting_first_audio": greeting_latency,
            "mark_playback_delay": (session.mark_playback_delay_ms if session else None),
        },
        close_code=session.close_code if session else None,
        started_at=started_wall.isoformat(),
        ended_at=ended_wall.isoformat(),
    )


def _failed_verdict(
    scenario: Scenario,
    run_uuid: str,
    started_at: datetime,
    reason: VerdictReason,
) -> ProbeVerdict:
    return ProbeVerdict(
        scenario=scenario,
        run_uuid=run_uuid,
        passed=False,
        reason=reason,
        checks={},
        counts={},
        latencies_ms={},
        close_code=None,
        started_at=started_at.isoformat(),
        ended_at=datetime.now(UTC).isoformat(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=[scenario.value for scenario in Scenario])
    parser.add_argument(
        "--urgent-variant",
        choices=[variant.value for variant in UrgentVariant],
        default=UrgentVariant.PRE_ARM.value,
    )
    parser.add_argument("--json-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    scenario = Scenario(args.scenario)
    run_uuid = str(uuid.uuid4())
    started_at = datetime.now(UTC)
    try:
        environment = ProbeEnvironment.from_environ(os.environ)
        verdict = asyncio.run(
            run_probe(
                environment,
                scenario=scenario,
                urgent_variant=UrgentVariant(args.urgent_variant),
            )
        )
    except ValueError:
        verdict = _failed_verdict(
            scenario,
            run_uuid,
            started_at,
            VerdictReason.CONFIGURATION_ERROR,
        )
    encoded = verdict.to_json()
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
