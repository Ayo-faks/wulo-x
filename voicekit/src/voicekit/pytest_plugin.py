"""pytest plugin exposing voicekit fixtures.

Registered via the ``pytest11`` entry point, so ``pip install voicekit-fakes``
makes ``virtual_clock``, ``voicelive_fake``, and ``twilio_stream`` available in
any test module without imports.
"""

from __future__ import annotations

import pytest

from voicekit.clock import VirtualClock
from voicekit.fakes.twilio_media_streams import FakeTwilioMediaStream
from voicekit.fakes.voicelive import FakeVoiceLiveConnection


@pytest.fixture
def virtual_clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture
def voicelive_fake(virtual_clock: VirtualClock) -> FakeVoiceLiveConnection:
    return FakeVoiceLiveConnection(clock=virtual_clock)


@pytest.fixture
def twilio_stream(virtual_clock: VirtualClock) -> FakeTwilioMediaStream:
    return FakeTwilioMediaStream(clock=virtual_clock)
