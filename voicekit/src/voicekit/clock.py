"""Deterministic virtual clock for timing assertions without real sleeps.

Voice-agent timing bugs (dead air, pacer delays) are about *when* audio moves,
but real ``asyncio.sleep`` makes such tests slow and flaky. ``VirtualClock``
gives tests a controllable timeline: code under test awaits ``clock.sleep()``,
and the test drives time forward with ``await clock.advance()``. All timestamps
are deterministic floats in virtual seconds.
"""

from __future__ import annotations

import asyncio
import heapq


class VirtualClock:
    """A manually-advanced clock with an awaitable ``sleep``."""

    def __init__(self) -> None:
        self._now = 0.0
        self._sequence = 0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []

    @property
    def now(self) -> float:
        """Current virtual time in seconds."""
        return self._now

    def elapsed_ms(self, since: float) -> float:
        """Milliseconds of virtual time elapsed since ``since``."""
        return (self._now - since) * 1000.0

    async def sleep(self, seconds: float) -> None:
        """Sleep in virtual time; resolves when ``advance`` passes the deadline."""
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self._now + seconds, self._sequence, future))
        self._sequence += 1
        await future

    async def advance(self, seconds: float) -> None:
        """Advance virtual time, waking sleepers in deadline order.

        Yields control before and after moving time so newly-created tasks can
        register their sleeps, and awakened tasks run (and may register new
        sleeps) before time moves further.
        """
        if seconds < 0:
            raise ValueError("cannot advance a clock backwards")
        await self._yield()  # let pending tasks register their sleeps first
        target = self._now + seconds
        while self._sleepers and self._sleepers[0][0] <= target + 1e-12:
            deadline, _, future = heapq.heappop(self._sleepers)
            self._now = max(self._now, deadline)
            if not future.done():
                future.set_result(None)
            await self._yield()
        self._now = target
        await self._yield()

    @staticmethod
    async def _yield() -> None:
        for _ in range(3):
            await asyncio.sleep(0)
