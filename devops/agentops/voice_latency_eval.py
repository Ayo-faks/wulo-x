#!/usr/bin/env python3
"""Deterministic per-turn latency eval for the inbound Twilio↔VoiceLive path.

Reconstructs a per-turn latency ledger from AppTraces log anchors and fails
(non-zero exit) when the caller-perceived latency thresholds are breached.
This is the latency proof for voice work; the hosted AgentOps gate only
evaluates the Foundry TEXT agent and can never measure audio latency.

Anchors (all INFO-level AppTraces messages):
  T0  "[Turn] Speech stopped | turn=N"                    user finished speaking
  T1  "[USER] Says: ..."                                  transcription completed
  T2  "Inbound Clinic voice safety routed to staff ..."   deterministic gate done
      "Clinic Recall safety response created ..."         safety response issued
  T3  "[Orchestrator] LLM TTFT | turn=N ttft_ms=X"        first model token
      "[Twilio] First audio delta | response=..."         first audio from model
  T4  "[Twilio] First audio chunk sent | delta_to_send_ms=X"  first audio to caller
      "[Twilio] Assistant: ..."                           transcript done (fallback)
  Greeting cold path: "Session ready:" → "[Greeting] Session-ready greeting
  response requested" → first audio chunk.

Usage:
  # Offline (canned az query output, CI-safe):
  python3 devops/agentops/voice_latency_eval.py --input rows.json

  # Live (queries Log Analytics via az CLI; requires `az login`):
  python3 devops/agentops/voice_latency_eval.py \
      --workspace 9bd9f81d-b9a0-4c08-87bd-6bd62ef4d933 \
      --start 2026-07-07T18:00:00Z --end 2026-07-07T19:35:00Z

Exit codes: 0 = thresholds met, 1 = threshold/anchor failure, 2 = usage/data error.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime

_RE_SESSION = re.compile(r"session=(\S+)")
_RE_TURN = re.compile(r"turn=(\d+)")
_RE_TTFT = re.compile(r"ttft_ms=([0-9.]+)")
_RE_CONNECT = re.compile(r"connect_ms=([0-9.]+)")
_RE_DELTA_SEND = re.compile(r"delta_to_send_ms=([-0-9.]+)")

_SAFETY_GATE_MARKERS = (
    "Inbound Clinic voice safety routed to staff",
    "Inbound Clinic mixed safety+booking request captured",
)
_SAFETY_RESPONSE_MARKER = "Clinic Recall safety response created"


def _parse_ts(value: str) -> datetime:
    v = value.rstrip("Z")
    # Log Analytics emits up to 7 fractional digits; datetime accepts 6.
    if "." in v:
        head, frac = v.split(".", 1)
        v = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(v)


def _ms(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() * 1000.0


@dataclass
class Turn:
    user_text: str = ""
    t0_speech_stopped: datetime | None = None
    t1_transcript: datetime | None = None
    t2_gate_done: datetime | None = None
    t2_safety_response: datetime | None = None
    t3_ttft_ms: float | None = None
    t3_first_delta: datetime | None = None
    t4_first_chunk: datetime | None = None
    t4_transcript_done: datetime | None = None
    is_safety_turn: bool = False

    @property
    def vad_stt_ms(self) -> float | None:
        if self.t0_speech_stopped and self.t1_transcript:
            return _ms(self.t1_transcript, self.t0_speech_stopped)
        return None

    @property
    def gate_ms(self) -> float | None:
        if self.t1_transcript and self.t2_gate_done:
            return _ms(self.t2_gate_done, self.t1_transcript)
        return None

    @property
    def first_audio_ms(self) -> float | None:
        """Caller-perceived gap: speech stopped → first audio chunk sent."""
        anchor = self.t4_first_chunk or None
        base = self.t0_speech_stopped or self.t1_transcript
        if anchor and base:
            return _ms(anchor, base)
        return None

    @property
    def transcript_done_ms(self) -> float | None:
        """Fallback proxy: speech stopped (or T1) → assistant transcript done."""
        base = self.t0_speech_stopped or self.t1_transcript
        if self.t4_transcript_done and base:
            return _ms(self.t4_transcript_done, base)
        return None

    @property
    def perceived_ms(self) -> float | None:
        return self.first_audio_ms if self.first_audio_ms is not None else self.transcript_done_ms

    @property
    def perceived_anchor(self) -> str:
        return "first_audio" if self.first_audio_ms is not None else "transcript_done"


@dataclass
class Call:
    session_id: str
    connect_ms: float | None = None
    session_ready: datetime | None = None
    greeting_requested: datetime | None = None
    greeting_first_audio: datetime | None = None
    greeting_transcript: datetime | None = None
    turns: list[Turn] = field(default_factory=list)

    @property
    def greeting_delay_ms(self) -> float | None:
        base = self.session_ready
        anchor = self.greeting_first_audio or self.greeting_transcript
        if base and anchor:
            return _ms(anchor, base)
        return None


def build_ledger(rows: list[dict]) -> list[Call]:
    """Group AppTraces rows into per-call, per-turn latency ledgers."""
    calls: list[Call] = []
    current: Call | None = None
    open_turn: Turn | None = None

    for row in rows:
        msg = row.get("Message", "") or ""
        ts = _parse_ts(row["TimeGenerated"])

        if "[Twilio] VoiceLive connected" in msg:
            m = _RE_SESSION.search(msg)
            current = Call(session_id=m.group(1) if m else f"call-{len(calls)}")
            cm = _RE_CONNECT.search(msg)
            if cm:
                current.connect_ms = float(cm.group(1))
            calls.append(current)
            open_turn = None
            continue
        if current is None:
            continue

        if msg.startswith("Session ready:"):
            current.session_ready = ts
        elif "[Greeting] Session-ready greeting response requested" in msg:
            current.greeting_requested = ts
        elif "[Turn] Speech stopped" in msg:
            open_turn = Turn(t0_speech_stopped=ts)
            current.turns.append(open_turn)
        elif msg.startswith("[USER] Says:"):
            if open_turn is None or open_turn.t1_transcript is not None:
                open_turn = Turn()
                current.turns.append(open_turn)
            open_turn.t1_transcript = ts
            open_turn.user_text = msg.split("[USER] Says:", 1)[1].strip()[:80]
        elif any(marker in msg for marker in _SAFETY_GATE_MARKERS):
            if open_turn is not None:
                open_turn.t2_gate_done = ts
                open_turn.is_safety_turn = True
        elif _SAFETY_RESPONSE_MARKER in msg:
            if open_turn is not None:
                open_turn.t2_safety_response = ts
        elif "[Orchestrator] LLM TTFT" in msg:
            m = _RE_TTFT.search(msg)
            if open_turn is not None and m:
                open_turn.t3_ttft_ms = float(m.group(1))
        elif "[Twilio] First audio delta" in msg:
            if open_turn is not None and open_turn.t3_first_delta is None:
                open_turn.t3_first_delta = ts
        elif "[Twilio] First audio chunk sent" in msg:
            if open_turn is not None and open_turn.t4_first_chunk is None:
                open_turn.t4_first_chunk = ts
            elif current.greeting_first_audio is None and not current.turns:
                current.greeting_first_audio = ts
        elif msg.startswith("[Twilio] Assistant:"):
            if open_turn is not None and open_turn.t1_transcript is not None:
                if open_turn.t4_transcript_done is None:
                    open_turn.t4_transcript_done = ts
            elif current.greeting_transcript is None and not current.turns:
                current.greeting_transcript = ts
    return calls


def render_table(calls: list[Call]) -> str:
    lines: list[str] = []
    for call in calls:
        lines.append(f"call {call.session_id}  connect_ms={call.connect_ms}")
        if call.greeting_delay_ms is not None:
            lines.append(f"  greeting: session_ready→first_audio {call.greeting_delay_ms:.0f} ms")
        header = (
            f"  {'turn':<5}{'vad+stt':>9}{'gate':>7}{'ttft':>7}"
            f"{'perceived':>11}{'anchor':>17}  utterance"
        )
        lines.append(header)
        for i, turn in enumerate(call.turns, 1):
            def fmt(v: float | None) -> str:
                return f"{v:.0f}" if v is not None else "-"
            lines.append(
                f"  {i:<5}{fmt(turn.vad_stt_ms):>9}{fmt(turn.gate_ms):>7}"
                f"{fmt(turn.t3_ttft_ms):>7}{fmt(turn.perceived_ms):>11}"
                f"{turn.perceived_anchor:>17}  {turn.user_text}"
            )
    return "\n".join(lines)


def evaluate(
    calls: list[Call],
    *,
    p50_max_ms: float,
    require_audio_anchor: bool = False,
    greeting_max_ms: float | None = None,
) -> tuple[bool, list[str]]:
    """Return (passed, failure_reasons)."""
    failures: list[str] = []
    perceived = [t.perceived_ms for c in calls for t in c.turns if t.perceived_ms is not None]
    if not perceived:
        failures.append("no turns with a perceived-latency anchor found")
        return False, failures
    p50 = statistics.median(perceived)
    if p50 > p50_max_ms:
        failures.append(f"perceived latency p50 {p50:.0f} ms exceeds max {p50_max_ms:.0f} ms")
    if require_audio_anchor:
        missing = [
            f"{c.session_id} turn {i}"
            for c in calls
            for i, t in enumerate(c.turns, 1)
            if t.perceived_ms is not None and t.first_audio_ms is None
        ]
        if missing:
            failures.append(f"turns missing first-audio anchor: {', '.join(missing)}")
    if greeting_max_ms is not None:
        for c in calls:
            delay = c.greeting_delay_ms
            if delay is not None and delay > greeting_max_ms:
                failures.append(
                    f"{c.session_id} greeting delay {delay:.0f} ms exceeds max {greeting_max_ms:.0f} ms"
                )
    # Safety invariant: on safety turns the deterministic gate must complete
    # before (or with) the spoken safety response — never after.
    for c in calls:
        for i, t in enumerate(c.turns, 1):
            if t.is_safety_turn and t.t2_safety_response and t.t2_gate_done:
                if t.t2_safety_response < t.t2_gate_done:
                    failures.append(
                        f"{c.session_id} turn {i}: safety response created before gate completed"
                    )
    return not failures, failures


def summary_stats(calls: list[Call]) -> dict:
    perceived = [t.perceived_ms for c in calls for t in c.turns if t.perceived_ms is not None]
    gates = [t.gate_ms for c in calls for t in c.turns if t.gate_ms is not None]
    return {
        "calls": len(calls),
        "turns": sum(len(c.turns) for c in calls),
        "perceived_p50_ms": round(statistics.median(perceived), 1) if perceived else None,
        "perceived_max_ms": round(max(perceived), 1) if perceived else None,
        "gate_p50_ms": round(statistics.median(gates), 1) if gates else None,
        "greeting_delays_ms": [
            round(c.greeting_delay_ms, 1) for c in calls if c.greeting_delay_ms is not None
        ],
    }


_KQL_TEMPLATE = (
    "AppTraces | where TimeGenerated between (datetime({start}) .. datetime({end})) "
    "| where Message startswith '[USER]' or Message startswith '[Twilio]' "
    "or Message startswith '[Turn]' or Message startswith '[Orchestrator]' "
    "or Message startswith '[Greeting]' or Message startswith 'Session ready' "
    "or Message has 'safety' or Message has 'connect_ms' "
    "| project TimeGenerated, Message | order by TimeGenerated asc"
)


def fetch_rows(workspace: str, start: str, end: str) -> list[dict]:
    query = _KQL_TEMPLATE.format(start=start, end=end)
    out = subprocess.run(
        ["az", "monitor", "log-analytics", "query", "-w", workspace,
         "--analytics-query", query, "-o", "json"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="offline JSON file of AppTraces rows (az query output)")
    parser.add_argument("--workspace", help="Log Analytics workspace id (live mode)")
    parser.add_argument("--start", help="ISO start time (live mode)")
    parser.add_argument("--end", help="ISO end time (live mode)")
    parser.add_argument("--p50-max-ms", type=float, default=1500.0)
    parser.add_argument("--greeting-max-ms", type=float, default=None)
    parser.add_argument("--require-audio-anchor", action="store_true")
    parser.add_argument("--json-out", help="write summary JSON to this path")
    args = parser.parse_args(argv)

    if args.input:
        with open(args.input) as fh:
            rows = json.load(fh)
    elif args.workspace and args.start and args.end:
        rows = fetch_rows(args.workspace, args.start, args.end)
    else:
        parser.error("provide --input OR --workspace/--start/--end")
        return 2

    calls = build_ledger(rows)
    if not calls:
        print("ERROR: no calls found in the supplied window", file=sys.stderr)
        return 2

    print(render_table(calls))
    stats = summary_stats(calls)
    print(f"\nsummary: {json.dumps(stats)}")
    passed, failures = evaluate(
        calls,
        p50_max_ms=args.p50_max_ms,
        require_audio_anchor=args.require_audio_anchor,
        greeting_max_ms=args.greeting_max_ms,
    )
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"summary": stats, "passed": passed, "failures": failures}, fh, indent=2)
    if not passed:
        for reason in failures:
            print(f"FAIL: {reason}", file=sys.stderr)
        return 1
    print("PASS: latency thresholds met")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
