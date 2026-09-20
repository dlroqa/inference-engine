"""A bounded, in-memory async event bus with a recent-event ring buffer.

Two channels back the operator WebSocket streams:

- ``metrics`` — periodic resource + counter snapshots (pushed by the sampler).
- ``feed`` — the inference live feed: request start/end, sampled token progress,
  and errors.

The bus is deliberately in-process and bounded. Each subscriber gets its own
fixed-size queue; if a slow consumer falls behind, its **oldest** buffered event
is dropped rather than blocking publishers or growing without limit. Each channel
also keeps a bounded ring buffer of recent events so a late-joining client
receives recent history before live events.

Multi-node event distribution (Redis/NATS) is a later, scale-time concern; this
stays a single-process bus by design (see the Block 4 scope).

Events carry only structured metadata — never raw prompts, responses, or secrets.
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


class EventChannel(enum.StrEnum):
    """Named pub/sub channels."""

    METRICS = "metrics"
    FEED = "feed"


class EventType(enum.StrEnum):
    """Feed event types (metrics snapshots use ``METRICS``)."""

    METRICS = "metrics"
    REQUEST_START = "request.start"
    REQUEST_PROGRESS = "request.progress"
    REQUEST_END = "request.end"
    REQUEST_ERROR = "request.error"


@dataclass(slots=True)
class Event:
    """One event on the bus.

    ``data`` is a plain JSON-serializable dict of structured metadata. Callers are
    responsible for never placing prompt/response text or secrets in it.
    """

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type.value, "ts": self.ts, **self.data}


class _Subscription:
    """A single subscriber's bounded queue with drop-oldest overflow behavior."""

    __slots__ = ("_queue", "maxsize", "dropped", "_event")

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._queue: deque[Event] = deque()
        self.dropped = 0
        self._event = asyncio.Event()

    def offer(self, event: Event) -> None:
        if self.maxsize > 0 and len(self._queue) >= self.maxsize:
            self._queue.popleft()
            self.dropped += 1
        self._queue.append(event)
        self._event.set()

    async def get(self) -> Event:
        while not self._queue:
            self._event.clear()
            await self._event.wait()
        return self._queue.popleft()


class EventBus:
    """In-process pub/sub with per-channel bounded history and subscribers."""

    def __init__(self, *, history: int = 200, subscriber_queue: int = 500) -> None:
        self._history_size = history
        self._subscriber_queue = subscriber_queue
        self._history: dict[EventChannel, deque[Event]] = {
            channel: deque(maxlen=history) for channel in EventChannel
        }
        self._subscribers: dict[EventChannel, set[_Subscription]] = {
            channel: set() for channel in EventChannel
        }

    def publish(self, channel: EventChannel, event: Event) -> None:
        """Record ``event`` in the channel history and fan it out to subscribers.

        Non-blocking: a full subscriber queue drops its oldest event. Safe to call
        from within the event loop (the normal case for request handlers and the
        sampler, which run on the same loop).
        """
        self._history[channel].append(event)
        for sub in self._subscribers[channel]:
            sub.offer(event)

    def recent(self, channel: EventChannel) -> list[Event]:
        """A snapshot copy of the channel's recent-event ring buffer."""
        return list(self._history[channel])

    def subscriber_count(self, channel: EventChannel) -> int:
        return len(self._subscribers[channel])

    async def subscribe(
        self, channel: EventChannel, *, replay_history: bool = True
    ) -> AsyncIterator[Event]:
        """Yield recent history (optionally) then live events until cancelled.

        The caller consumes this as an async generator; cancelling/closing it
        unsubscribes and frees the queue.
        """
        sub = _Subscription(self._subscriber_queue)
        self._subscribers[channel].add(sub)
        try:
            if replay_history:
                for event in list(self._history[channel]):
                    yield event
            while True:
                yield await sub.get()
        finally:
            self._subscribers[channel].discard(sub)
