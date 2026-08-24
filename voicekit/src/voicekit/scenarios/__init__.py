"""Scenario assertions for the five production-proven voice-agent failure modes.

Each helper inspects fake timelines/ledgers and raises :class:`ScenarioFailure`
with a message written in voice-agent terms (dead air, barge-in, stale cancel)
rather than fake internals. Every assertion here corresponds to a real
production incident; see the eval corpus for the paired good/bad reference
agents that keep these detectors honest.
"""

from __future__ import annotations

from voicekit.fakes.twilio_media_streams import FakeTwilioMediaStream
from voicekit.fakes.voicelive import FakeVoiceLiveConnection


class ScenarioFailure(AssertionError):
    """A voice-agent failure mode was detected."""


def assert_no_concurrent_responses(conn: FakeVoiceLiveConnection) -> None:
    """C2 — concurrent ``response.create`` race.

    Two responses in flight at once on a realtime session either errors
    ('conversation already has an active response') or produces overlapping /
    dropped audio. Agents must serialize or coalesce response creation.
    """
    peak = conn.response.max_concurrent_responses
    if peak > 1:
        raise ScenarioFailure(
            f"response race: {peak} responses were in flight concurrently. "
            "Serialize response.create behind a lock or coalesce duplicate requests — "
            "on a live call this races the server and one reply dies silently."
        )


def assert_no_stale_cancel(conn: FakeVoiceLiveConnection) -> None:
    """C3 — cancel targeted a response that was already done (stale cancel).

    After a barge-in, cancelling 'whatever is active' can kill the NEXT
    response if the original already completed. Cancels must target a live
    response id.
    """
    done: set[str | None] = set()
    for entry in conn.timeline.entries:
        if entry.kind == "response.done":
            done.add(entry.detail.get("response_id"))
        elif entry.kind == "response.cancel":
            target = entry.detail.get("target")
            if target is None:
                raise ScenarioFailure(
                    "stale cancel: response.cancel was issued with no response in flight. "
                    "Track the in-flight response id and skip the cancel when it has already completed."
                )
            if target in done:
                raise ScenarioFailure(
                    f"stale cancel: response.cancel targeted {target!r} which had already completed. "
                    "A late cancel races the next response and can kill the wrong reply."
                )


def assert_ordering(conn: FakeVoiceLiveConnection, *, gate: str, response_kind: str = "response.create") -> None:
    """C4 — a gated response must never be created before its gate completes.

    ``gate`` is a domain milestone recorded via ``conn.mark_event`` (e.g.
    ``"safety_gate.done"``). If ``response_kind`` appears earlier in the
    timeline, the agent spoke before the deterministic check finished.
    """
    gate_idx = conn.timeline.index_of(gate)
    resp_idx = conn.timeline.index_of(response_kind)
    if resp_idx is None:
        return
    if gate_idx is None or resp_idx < gate_idx:
        raise ScenarioFailure(
            f"ordering violation: {response_kind} happened before {gate!r} completed. "
            "The agent must not speak until the deterministic gate has finished — "
            "escalation wording and call disposition depend on its outcome."
        )


def assert_first_audio_within(stream: FakeTwilioMediaStream, *, max_ms: float) -> None:
    """C5 — dead air / delayed first audio.

    From the trigger (``stream.set_trigger()``) to the first ``media`` chunk
    must be at most ``max_ms`` of virtual time. Pacer bugs that sleep before
    dequeuing the first chunk add flat per-reply latency the caller hears as
    hesitation — or, if audio never arrives, dead air.
    """
    delay = stream.first_media_delay_ms()
    if delay is None:
        raise ScenarioFailure(
            "dead air: no audio was ever sent to the caller after the trigger. "
            "Check for swallowed exceptions on the send path — a failed create/send "
            "logged at DEBUG is silence on a live call."
        )
    if delay > max_ms:
        raise ScenarioFailure(
            f"delayed first audio: {delay:.1f}ms from trigger to first chunk "
            f"(budget {max_ms:.0f}ms). Send the first available chunk immediately; "
            "never sleep before the first dequeue."
        )


def assert_silence_after_clear(stream: FakeTwilioMediaStream, *, grace_ms: float = 0.0) -> None:
    """Barge-in hygiene: no audio may flow after ``clear`` beyond a grace window."""
    if not stream.clears:
        return
    first_clear = stream.clears[0]
    late = [c for c in stream.media_after_clear if (c.at - first_clear) * 1000.0 > grace_ms]
    if late:
        raise ScenarioFailure(
            f"stale audio after barge-in: {len(late)} media chunk(s) sent after clear "
            f"(first at +{(late[0].at - first_clear) * 1000.0:.1f}ms, grace {grace_ms:.0f}ms). "
            "Stop the pacer and drop buffered audio when the caller barges in."
        )


__all__ = [
    "ScenarioFailure",
    "assert_first_audio_within",
    "assert_no_concurrent_responses",
    "assert_no_stale_cancel",
    "assert_ordering",
    "assert_silence_after_clear",
]
