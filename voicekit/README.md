# voicekit

**Offline deterministic tests for realtime voice-agent transport bugs — catch dead air before your users do.**

Voice agents fail in ways text evals structurally cannot catch: an SDK keyword
that silently kills your safety message, two `response.create` calls racing
after concurrent escalations, a stale cancel murdering the wrong reply, audio
still flushing after the caller barged in. These bugs pass every conversation
eval and only surface on live phone calls.

`voicekit` gives you **strict, signature-pinned fakes** and **deterministic
failure-mode assertions** that run in plain pytest — no LLM, no phone call, no
network.

## The bug this exists for

A production clinic voice agent called:

```python
await conn.response.create(instructions=SAFETY_LINE)   # tests green ✅
```

The test fake accepted `**kwargs`. The real Azure VoiceLive SDK is
keyword-only with exactly `response` / `event_id` / `additional_instructions` —
so on every live call this raised a client-side `TypeError`, was swallowed at
DEBUG, and the safety message **never played, for 7 days**. Dead air.

With voicekit the same code fails in your test suite:

```python
from voicekit import FakeVoiceLiveConnection

conn = FakeVoiceLiveConnection()
await conn.response.create(instructions=SAFETY_LINE)
# TypeError: create() got an unexpected keyword argument 'instructions'
```

## What's in the box

- **`voicekit.fakes.voicelive`** — fake of the `azure.ai.voicelive.aio`
  resource surface, pinned to the real SDK signatures, with an action timeline
  and an in-flight response ledger.
- **`voicekit.fakes.twilio_media_streams`** — fake Twilio far end with a
  virtual-clock pacing ledger (first-audio delay, throughput, barge-in `clear`).
- **`voicekit.scenarios`** — assertions for the production-proven failure modes:
  - `assert_no_concurrent_responses` — response.create races
  - `assert_no_stale_cancel` — cancels targeting completed responses
  - `assert_ordering` — speaking before a deterministic gate finished
  - `assert_first_audio_within` — dead air / delayed first audio
  - `assert_silence_after_clear` — stale audio after barge-in
- **`voicekit.strict`** — `assert_conforms(fake, real)`: verify *your own*
  fakes against the installed SDK; flags `**kwargs`, renamed kwargs, dropped
  keyword-only markers, sync/async mismatches, requiredness changes.
- **`voicekit.clock.VirtualClock`** — deterministic time; no real sleeps.
- **pytest fixtures** — `virtual_clock`, `voicelive_fake`, `twilio_stream`.

## Example

```python
import pytest
from voicekit.scenarios import assert_first_audio_within, assert_no_concurrent_responses

async def test_reply_is_not_dead_air(twilio_stream, virtual_clock):
    twilio_stream.set_trigger()                      # user stopped speaking
    await run_my_agent_turn(twilio_stream, virtual_clock)
    assert_first_audio_within(twilio_stream, max_ms=100)

async def test_double_escalation_is_coalesced(voicelive_fake):
    await asyncio.gather(my_escalate(voicelive_fake), my_escalate(voicelive_fake))
    assert_no_concurrent_responses(voicelive_fake)
```

## How we know voicekit itself works

Every release must pass the eval gate (`evals/run_evals.py`): a corpus of
paired reference agents — one reproducing each real production incident, one
corrected — where every detector must **fail the buggy agent and pass the good
one** (0 false negatives, 0 false positives), deterministically across shuffled
repeats. Contract tests verify the fakes against the *installed* SDK, so
upstream signature drift is caught in CI, not on your production calls.

```
EVAL GATE GREEN: 6/6 caught, 0 false positives, deterministic across 20 shuffled runs
```

## Install

```bash
pip install voicekit-fakes           # fakes + scenarios + pytest plugin (zero deps)
pip install 'voicekit-fakes[contract]'  # + azure-ai-voicelive for contract drift tests
```

## Status

v0.1 (alpha): Azure VoiceLive + Twilio Media Streams. Planned: OpenAI Realtime
fake (v0.2), latency-ledger CI gate with structured turn anchors (v0.3).

License: MIT
