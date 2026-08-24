"""voicekit — offline deterministic test fakes and scenarios for realtime voice agents.

Catch dead air, response races, and SDK signature mismatches before a real phone call.
"""

from voicekit.clock import VirtualClock
from voicekit.fakes.twilio_media_streams import MULAW_BYTES_PER_SECOND, FakeTwilioMediaStream, MediaChunk
from voicekit.fakes.voicelive import FakeVoiceLiveConnection
from voicekit.scenarios import ScenarioFailure
from voicekit.strict import ConformanceError, assert_conforms, assert_object_conforms

__version__ = "0.1.0.dev0"

__all__ = [
    "MULAW_BYTES_PER_SECOND",
    "ConformanceError",
    "FakeTwilioMediaStream",
    "FakeVoiceLiveConnection",
    "MediaChunk",
    "ScenarioFailure",
    "VirtualClock",
    "assert_conforms",
    "assert_object_conforms",
    "__version__",
]
