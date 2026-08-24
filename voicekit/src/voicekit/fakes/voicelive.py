"""Strict fake for the Azure VoiceLive async connection surface.

Extracted and generalized from a production voice agent's test suite. Every
resource method is pinned to the real ``azure.ai.voicelive.aio`` signature
(keyword-only, exact parameter names). A live incident proved that a permissive
``**kwargs`` fake let ``response.create(instructions=...)`` pass tests while
the real SDK raised ``TypeError`` client-side — the deterministic safety
message never played and the call sat in silence. Do not loosen these fakes.

The connection keeps a :class:`Timeline` of every action so scenario helpers
can assert ordering, coalescing, and cancellation-target correctness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Union

from voicekit.clock import VirtualClock


@dataclass(frozen=True)
class TimelineEntry:
    at: float
    kind: str
    detail: Mapping[str, Any] = field(default_factory=dict)


class Timeline:
    """Ordered record of everything that happened on the fake connection."""

    def __init__(self, clock: VirtualClock | None) -> None:
        self._clock = clock
        self._entries: list[TimelineEntry] = []
        self._counter = 0

    def record(self, kind: str, **detail: Any) -> TimelineEntry:
        at = self._clock.now if self._clock is not None else float(self._counter)
        self._counter += 1
        entry = TimelineEntry(at=at, kind=kind, detail=detail)
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> list[TimelineEntry]:
        return list(self._entries)

    def kinds(self) -> list[str]:
        return [e.kind for e in self._entries]

    def first(self, kind: str) -> TimelineEntry | None:
        return next((e for e in self._entries if e.kind == kind), None)

    def index_of(self, kind: str) -> int | None:
        for i, e in enumerate(self._entries):
            if e.kind == kind:
                return i
        return None


class FakeResponseResource:
    """Pinned to ``azure.ai.voicelive.aio`` ``ResponseResource`` (create/cancel)."""

    def __init__(self, timeline: Timeline) -> None:
        self._timeline = timeline
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[dict[str, Any]] = []
        self._create_failures: list[BaseException] = []
        self._next_response_id = 0
        self.active_response_ids: list[str] = []
        self.max_concurrent_responses = 0

    def fail_next_create(self, exc: BaseException) -> None:
        """Script the next ``create`` call to raise (e.g. an active-response race)."""
        self._create_failures.append(exc)

    async def create(
        self,
        *,
        response: Optional[Union[Mapping[str, Any], Any]] = None,
        event_id: Optional[str] = None,
        additional_instructions: Optional[str] = None,
    ) -> None:
        if self._create_failures:
            exc = self._create_failures.pop(0)
            self._timeline.record("response.create_failed", error=repr(exc))
            raise exc
        kwargs: dict[str, Any] = {}
        if response is not None:
            kwargs["response"] = response
        if event_id is not None:
            kwargs["event_id"] = event_id
        if additional_instructions is not None:
            kwargs["additional_instructions"] = additional_instructions
        self.created.append(kwargs)
        self._next_response_id += 1
        response_id = f"resp_{self._next_response_id}"
        self.active_response_ids.append(response_id)
        self.max_concurrent_responses = max(self.max_concurrent_responses, len(self.active_response_ids))
        self._timeline.record("response.create", response_id=response_id, **kwargs)

    async def cancel(
        self,
        *,
        response_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if response_id is not None:
            kwargs["response_id"] = response_id
        if event_id is not None:
            kwargs["event_id"] = event_id
        self.cancelled.append(kwargs)
        target = response_id if response_id is not None else (self.active_response_ids[-1] if self.active_response_ids else None)
        if target in self.active_response_ids:
            self.active_response_ids.remove(target)
        self._timeline.record("response.cancel", target=target, **kwargs)

    def complete_response(self, response_id: Optional[str] = None) -> None:
        """Mark a response done (as the server would via ``response.done``)."""
        target = response_id if response_id is not None else (self.active_response_ids[0] if self.active_response_ids else None)
        if target in self.active_response_ids:
            self.active_response_ids.remove(target)
        self._timeline.record("response.done", response_id=target)


class FakeConversationItemResource:
    """Pinned to ``ConversationItemResource`` (create/delete/retrieve/truncate)."""

    def __init__(self, timeline: Timeline) -> None:
        self._timeline = timeline
        self.created: list[Any] = []

    async def create(
        self,
        *,
        item: Union[Mapping[str, Any], Any],
        previous_item_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> None:
        self.created.append(item)
        self._timeline.record("conversation.item.create")

    async def delete(self, *, item_id: str, event_id: Optional[str] = None) -> None:
        self._timeline.record("conversation.item.delete", item_id=item_id)

    async def retrieve(self, *, item_id: str, event_id: Optional[str] = None) -> None:
        self._timeline.record("conversation.item.retrieve", item_id=item_id)

    async def truncate(
        self,
        *,
        item_id: str,
        audio_end_ms: int,
        content_index: int,
        event_id: Optional[str] = None,
    ) -> None:
        self._timeline.record("conversation.item.truncate", item_id=item_id, audio_end_ms=audio_end_ms)


class FakeConversationResource:
    def __init__(self, timeline: Timeline) -> None:
        self.item = FakeConversationItemResource(timeline)


class FakeSessionResource:
    """Pinned to ``SessionResource.update``."""

    def __init__(self, timeline: Timeline) -> None:
        self._timeline = timeline
        self.updates: list[Any] = []

    async def update(
        self,
        *,
        session: Union[Mapping[str, Any], Any],
        event_id: Optional[str] = None,
    ) -> None:
        self.updates.append(session)
        self._timeline.record("session.update")


class FakeInputAudioBufferResource:
    """Pinned to ``InputAudioBufferResource`` (append/clear/commit)."""

    def __init__(self, timeline: Timeline) -> None:
        self._timeline = timeline
        self.appended: list[str] = []

    async def append(self, *, audio: str, event_id: Optional[str] = None) -> None:
        self.appended.append(audio)
        self._timeline.record("input_audio_buffer.append", size=len(audio))

    async def clear(self, *, event_id: Optional[str] = None) -> None:
        self._timeline.record("input_audio_buffer.clear")

    async def commit(self, *, event_id: Optional[str] = None) -> None:
        self._timeline.record("input_audio_buffer.commit")


class FakeOutputAudioBufferResource:
    """Pinned to ``OutputAudioBufferResource.clear``."""

    def __init__(self, timeline: Timeline) -> None:
        self._timeline = timeline

    async def clear(self, *, event_id: Optional[str] = None) -> None:
        self._timeline.record("output_audio_buffer.clear")


class FakeVoiceLiveConnection:
    """Fake of the ``VoiceLiveConnection`` resource surface with an action timeline.

    Use :meth:`mark_event` to record domain milestones (e.g. ``safety_gate.done``)
    so scenario helpers can assert ordering invariants against SDK actions.
    """

    def __init__(self, clock: VirtualClock | None = None) -> None:
        self.timeline = Timeline(clock)
        self.response = FakeResponseResource(self.timeline)
        self.session = FakeSessionResource(self.timeline)
        self.conversation = FakeConversationResource(self.timeline)
        self.input_audio_buffer = FakeInputAudioBufferResource(self.timeline)
        self.output_audio_buffer = FakeOutputAudioBufferResource(self.timeline)
        self.closed = False

    def mark_event(self, name: str, **detail: Any) -> None:
        self.timeline.record(name, **detail)

    async def close(self) -> None:
        self.closed = True
        self.timeline.record("connection.close")
