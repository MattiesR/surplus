"""In-process event bus (plan §3). Subscribers get their own bounded queue; slow ones drop old events."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self, maxsize: int = 100) -> None:
        self._subs: set[asyncio.Queue[Event]] = set()
        self._maxsize = maxsize

    def publish(self, event: Event) -> None:
        for q in list(self._subs):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(event)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        q: asyncio.Queue[Event] = asyncio.Queue(self._maxsize)
        self._subs.add(q)
        try:
            yield q
        finally:
            self._subs.discard(q)
