"""Tests for the deterministic voice latency eval (devops/agentops/voice_latency_eval.py).

Uses canned AppTraces rows so the eval parser is CI-runnable without Azure.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "devops" / "agentops" / "voice_latency_eval.py"
)
_spec = importlib.util.spec_from_file_location("voice_latency_eval", _MODULE_PATH)
vle = importlib.util.module_from_spec(_spec)
sys.modules["voice_latency_eval"] = vle  # dataclasses require sys.modules registration
_spec.loader.exec_module(vle)


def _row(ts: str, msg: str) -> dict:
    return {"TimeGenerated": ts, "Message": msg}


SESSION = "inbound-call-abc123"


def _call_rows() -> list[dict]:
    """One call: greeting + a normal turn + a safety turn (all anchors present)."""
    return [
        _row("2026-07-07T19:27:21.120Z", f"[Twilio] VoiceLive connected | connect_ms=587.0 session={SESSION}"),
        _row("2026-07-07T19:27:21.477Z", "Session ready: sess_x | voice={'name': 'en-GB-SoniaNeural'}"),
        _row("2026-07-07T19:27:21.500Z", "[Greeting] Session-ready greeting response requested | agent=InboundClinicAgent"),
        _row("2026-07-07T19:27:23.000Z", f"[Twilio] First audio chunk sent | delta_to_send_ms=250.0 session={SESSION}"),
        _row("2026-07-07T19:27:25.753Z", f"[Twilio] Assistant: Hello, thanks for calling. | session={SESSION}"),
        # Normal turn
        _row("2026-07-07T19:27:27.000Z", "[Turn] Speech stopped | turn=1 agent=InboundClinicAgent"),
        _row("2026-07-07T19:27:28.030Z", "[USER] Says: Thank you."),
        _row("2026-07-07T19:27:28.500Z", "[Orchestrator] LLM TTFT | turn=1 ttft_ms=1500.00 agent=InboundClinicAgent"),
        _row("2026-07-07T19:27:28.600Z", f"[Twilio] First audio delta | response=resp_1 session={SESSION}"),
        _row("2026-07-07T19:27:28.900Z", f"[Twilio] First audio chunk sent | delta_to_send_ms=300.0 session={SESSION}"),
        _row("2026-07-07T19:27:34.736Z", f"[Twilio] Assistant: I'm happy to assist you. | session={SESSION}"),
        # Safety turn (clinical + booking)
        _row("2026-07-07T19:27:43.500Z", "[Turn] Speech stopped | turn=2 agent=InboundClinicAgent"),
        _row("2026-07-07T19:27:44.583Z", "[USER] Says: I'm having a headache and I want an appointment."),
        _row("2026-07-07T19:27:44.746Z", "Inbound Clinic voice safety routed to staff | intent=clinical success=True reason=clinical"),
        _row("2026-07-07T19:27:44.756Z", "Inbound Clinic mixed safety+booking request captured | reason=clinical success=True"),
        _row("2026-07-07T19:27:44.800Z", "Clinic Recall safety response created | intent=clinical"),
        _row("2026-07-07T19:27:45.900Z", f"[Twilio] First audio delta | response=resp_2 session={SESSION}"),
        _row("2026-07-07T19:27:46.200Z", f"[Twilio] First audio chunk sent | delta_to_send_ms=280.0 session={SESSION}"),
        _row("2026-07-07T19:27:57.487Z", f"[Twilio] Assistant: I've recorded your request. | session={SESSION}"),
    ]


class TestBuildLedger:
    def test_groups_one_call_with_connect_ms(self):
        calls = vle.build_ledger(_call_rows())
        assert len(calls) == 1
        assert calls[0].session_id == SESSION
        assert calls[0].connect_ms == 587.0

    def test_turn_count_excludes_greeting(self):
        calls = vle.build_ledger(_call_rows())
        assert len(calls[0].turns) == 2

    def test_greeting_delay_uses_first_audio_chunk(self):
        call = vle.build_ledger(_call_rows())[0]
        # 19:27:21.477 → 19:27:23.000 = 1523 ms
        assert call.greeting_delay_ms is not None
        assert abs(call.greeting_delay_ms - 1523.0) < 1.0

    def test_normal_turn_gaps(self):
        turn = vle.build_ledger(_call_rows())[0].turns[0]
        assert abs(turn.vad_stt_ms - 1030.0) < 1.0          # T0→T1
        assert turn.t3_ttft_ms == 1500.0                      # from log value
        assert abs(turn.first_audio_ms - 1900.0) < 1.0       # T0→first chunk
        assert turn.perceived_anchor == "first_audio"
        assert not turn.is_safety_turn

    def test_safety_turn_gate_and_ordering(self):
        turn = vle.build_ledger(_call_rows())[0].turns[1]
        assert turn.is_safety_turn
        # T1 19:27:44.583 → gate done 19:27:44.756 = 173 ms
        assert abs(turn.gate_ms - 173.0) < 1.0
        assert turn.t2_safety_response is not None
        assert turn.t2_safety_response >= turn.t2_gate_done

    def test_transcript_done_fallback_when_no_audio_anchor(self):
        rows = [
            _row("2026-07-07T19:04:13.735Z", f"[Twilio] VoiceLive connected | connect_ms=530.8 session={SESSION}"),
            _row("2026-07-07T19:04:27.363Z", "[USER] Says: I have a cough."),
            _row("2026-07-07T19:04:33.000Z", f"[Twilio] Assistant: reply text | session={SESSION}"),
        ]
        turn = vle.build_ledger(rows)[0].turns[0]
        assert turn.first_audio_ms is None
        assert turn.perceived_anchor == "transcript_done"
        assert abs(turn.perceived_ms - 5637.0) < 1.0

    def test_rows_before_any_call_are_ignored(self):
        rows = [_row("2026-07-07T18:00:00.000Z", "[USER] Says: orphan row")] + _call_rows()
        calls = vle.build_ledger(rows)
        assert len(calls) == 1
        assert len(calls[0].turns) == 2


class TestEvaluate:
    def test_passes_under_threshold(self):
        calls = vle.build_ledger(_call_rows())
        # Canned turns: 1900 ms and 2700 ms → p50 = 2300 ms
        passed, failures = vle.evaluate(calls, p50_max_ms=2500.0)
        assert passed, failures

    def test_fails_over_threshold(self):
        calls = vle.build_ledger(_call_rows())
        passed, failures = vle.evaluate(calls, p50_max_ms=100.0)
        assert not passed
        assert any("p50" in f for f in failures)

    def test_require_audio_anchor_flags_fallback_turns(self):
        rows = [
            _row("2026-07-07T19:04:13.735Z", f"[Twilio] VoiceLive connected | connect_ms=530.8 session={SESSION}"),
            _row("2026-07-07T19:04:27.363Z", "[USER] Says: hello"),
            _row("2026-07-07T19:04:28.000Z", f"[Twilio] Assistant: hi | session={SESSION}"),
        ]
        calls = vle.build_ledger(rows)
        passed, failures = vle.evaluate(calls, p50_max_ms=5000.0, require_audio_anchor=True)
        assert not passed
        assert any("first-audio anchor" in f for f in failures)

    def test_greeting_threshold(self):
        calls = vle.build_ledger(_call_rows())
        passed, failures = vle.evaluate(calls, p50_max_ms=5000.0, greeting_max_ms=1000.0)
        assert not passed
        assert any("greeting delay" in f for f in failures)

    def test_no_turns_fails(self):
        passed, failures = vle.evaluate([], p50_max_ms=1500.0)
        assert not passed

    def test_safety_response_before_gate_fails(self):
        rows = [
            _row("2026-07-07T19:27:21.120Z", f"[Twilio] VoiceLive connected | connect_ms=587.0 session={SESSION}"),
            _row("2026-07-07T19:27:44.583Z", "[USER] Says: chest pain"),
            # Spoken safety response BEFORE the deterministic gate completes → violation
            _row("2026-07-07T19:27:44.600Z", "Clinic Recall safety response created | intent=clinical"),
            _row("2026-07-07T19:27:44.746Z", "Inbound Clinic voice safety routed to staff | intent=clinical success=True reason=urgent"),
            _row("2026-07-07T19:27:45.000Z", f"[Twilio] First audio chunk sent | delta_to_send_ms=250.0 session={SESSION}"),
        ]
        calls = vle.build_ledger(rows)
        passed, failures = vle.evaluate(calls, p50_max_ms=5000.0)
        assert not passed
        assert any("before gate completed" in f for f in failures)


class TestSummaryStats:
    def test_summary_shape(self):
        calls = vle.build_ledger(_call_rows())
        stats = vle.summary_stats(calls)
        assert stats["calls"] == 1
        assert stats["turns"] == 2
        assert stats["perceived_p50_ms"] is not None
        assert stats["gate_p50_ms"] is not None
        assert len(stats["greeting_delays_ms"]) == 1
