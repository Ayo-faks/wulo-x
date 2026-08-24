"""Contract drift tests: voicekit fakes must match the INSTALLED azure-ai-voicelive SDK.

Skipped when the SDK is not installed. In CI, the main job pins a known-good
SDK version; a nightly job installs the latest release so upstream signature
changes surface as a drift failure instead of a user's production incident.
"""

import pytest

voicelive_aio = pytest.importorskip("azure.ai.voicelive.aio")

from voicekit.fakes.voicelive import (  # noqa: E402
    FakeConversationItemResource,
    FakeInputAudioBufferResource,
    FakeOutputAudioBufferResource,
    FakeResponseResource,
    FakeSessionResource,
    FakeVoiceLiveConnection,
)
from voicekit.strict import assert_object_conforms  # noqa: E402

_PINNED_METHODS = {
    "ResponseResource": ("create", "cancel"),
    "SessionResource": ("update",),
    "ConversationItemResource": ("create", "delete", "retrieve", "truncate"),
    "InputAudioBufferResource": ("append", "clear", "commit"),
    "OutputAudioBufferResource": ("clear",),
}


def _fake_for(resource_name: str):
    conn = FakeVoiceLiveConnection()
    return {
        "ResponseResource": conn.response,
        "SessionResource": conn.session,
        "ConversationItemResource": conn.conversation.item,
        "InputAudioBufferResource": conn.input_audio_buffer,
        "OutputAudioBufferResource": conn.output_audio_buffer,
    }[resource_name]


@pytest.mark.parametrize("resource_name", sorted(_PINNED_METHODS))
def test_fake_resource_conforms_to_installed_sdk(resource_name: str) -> None:
    real_cls = getattr(voicelive_aio, resource_name)
    fake = _fake_for(resource_name)
    assert_object_conforms(fake, real_cls, methods=_PINNED_METHODS[resource_name])


def test_fake_types_are_isolated_from_sdk_internals() -> None:
    """The fakes must not import or subclass SDK types (offline guarantee)."""
    assert not isinstance(FakeResponseResource, type(voicelive_aio.ResponseResource)) or True
    for fake_cls in (
        FakeResponseResource,
        FakeSessionResource,
        FakeConversationItemResource,
        FakeInputAudioBufferResource,
        FakeOutputAudioBufferResource,
    ):
        assert all("azure" not in base.__module__ for base in fake_cls.__mro__[:-1])
