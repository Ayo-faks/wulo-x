"""Machine-enforced contract: hand-written VoiceLive test fakes must match the installed SDK.

Until 2026-07-08 the pinning of the fakes in ``test_voicelive_clinic_recall_safety.py``
was enforced only by a docstring ("Do not loosen"). A loosened ``**kwargs`` fake
previously masked a client-side TypeError for 7 days of production dead air.
These tests make the pin executable: if anyone loosens a fake, or an
``azure-ai-voicelive`` upgrade changes a resource signature, this file fails in CI
instead of the bug surfacing on a live call.

Uses voicekit (developed in ./voicekit, pending extraction to its own package).
"""

import sys
from pathlib import Path

import pytest

_VOICEKIT_SRC = Path(__file__).resolve().parents[1] / "voicekit" / "src"
if str(_VOICEKIT_SRC) not in sys.path:  # temporary until voicekit ships on PyPI
    sys.path.insert(0, str(_VOICEKIT_SRC))

voicelive_aio = pytest.importorskip("azure.ai.voicelive.aio")
voicekit_strict = pytest.importorskip("voicekit.strict")

from tests.test_voicelive_clinic_recall_safety import (  # noqa: E402
    _ConversationItem,
    _CreateRaceResponse,
    _Response,
)


@pytest.mark.parametrize(
    ("fake", "real_cls", "methods"),
    [
        (_Response(), voicelive_aio.ResponseResource, ("create", "cancel")),
        (_CreateRaceResponse(), voicelive_aio.ResponseResource, ("create", "cancel")),
        (_ConversationItem(), voicelive_aio.ConversationItemResource, ("create",)),
    ],
    ids=["_Response", "_CreateRaceResponse", "_ConversationItem"],
)
def test_hand_written_fake_conforms_to_installed_sdk(fake, real_cls, methods) -> None:
    voicekit_strict.assert_object_conforms(fake, real_cls, methods=methods)


def test_loosened_fake_would_be_rejected() -> None:
    """Negative control: the pre-incident ``**kwargs`` fake shape must fail."""

    class LoosenedResponse:
        async def create(self, **kwargs) -> None: ...
        async def cancel(self, **kwargs) -> None: ...

    with pytest.raises(voicekit_strict.ConformanceError, match=r"\*\*kwargs"):
        voicekit_strict.assert_object_conforms(
            LoosenedResponse(), voicelive_aio.ResponseResource, methods=("create", "cancel")
        )
