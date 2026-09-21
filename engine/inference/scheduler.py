"""Admission control and bounded concurrency for the inference backend.

Block 1's backend enforces a single in-flight generation per model; Block 2
surfaced that as an immediate ``model_busy`` rejection for any overlapping
request. That fails, but not *gracefully*: a burst of clients gets errors even
though the engine could serve them one after another.

The :class:`Scheduler` is the honest fix. It sits between the API edge and the
backend as a small **admission layer**:

- **Bounded concurrency.** ``max_concurrency`` slots gate how many generations run
  at once. For the Tier-1 llama.cpp runtime a single model context serializes
  decoding, so the honest default is ``1`` — the scheduler does not fake
  parallelism it cannot deliver (see ``docs/concurrency.md``).
- **Bounded waiting.** When every slot is busy, requests wait for one to free up,
  but only up to ``max_queue_depth`` waiters and ``queue_timeout_s`` seconds.
  Beyond either bound the request is rejected with an explicit, retriable
  saturation signal instead of blocking forever or exhausting memory.
- **Observability.** Queue wait time, active work, queue depth, admissions,
  rejections, cancellations, and slow-consumer events are all counted so the
  behavior is visible on the operator metrics snapshot.

Everything here runs on the asyncio event loop, so the counters are plain ints
mutated without locks; the only suspension point is waiting for a slot.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class SchedulerSaturated(Exception):
    """Raised when a request cannot be admitted under the concurrency policy.

    ``reason`` is ``"queue_full"`` (no waiting capacity left) or ``"queue_timeout"``
    (waited longer than ``queue_timeout_s`` without a slot). ``retry_after_s`` is a
    best-effort backoff hint for the client.
    """

    def __init__(self, reason: str, *, retry_after_s: int) -> None:
        super().__init__(f"engine saturated ({reason})")
        self.reason = reason
        self.retry_after_s = retry_after_s


@dataclass(slots=True)
class SchedulerLease:
    """A held concurrency slot. Released exactly once when the work is done."""

    _scheduler: Scheduler
    waited_s: float
    _released: bool = False

    def release(self, *, cancelled: bool = False) -> None:
        if self._released:
            return
        self._released = True
        self._scheduler._release(cancelled=cancelled)


class Scheduler:
    """Bounded-concurrency admission control with a bounded waiting queue."""

    def __init__(
        self,
        *,
        max_concurrency: int = 1,
        max_queue_depth: int = 32,
        queue_timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if max_queue_depth < 0:
            raise ValueError("max_queue_depth must be >= 0")
        if queue_timeout_s < 0:
            raise ValueError("queue_timeout_s must be >= 0")
        self._max_concurrency = max_concurrency
        self._max_queue_depth = max_queue_depth
        self._queue_timeout_s = queue_timeout_s
        self._clock = clock
        self._sem = asyncio.Semaphore(max_concurrency)

        # Live gauges.
        self._in_use = 0
        self._waiters = 0
        # Cumulative counters.
        self._admitted = 0
        self._rejected_queue_full = 0
        self._rejected_timeout = 0
        self._cancelled = 0
        self._slow_consumer = 0
        self._peak_in_use = 0
        self._peak_waiters = 0
        # Wait-time aggregates (seconds).
        self._wait_total = 0.0
        self._wait_count = 0
        self._wait_max = 0.0
        self._wait_last = 0.0

    # -- admission ---------------------------------------------------------

    async def admit(self) -> SchedulerLease:
        """Acquire a slot, waiting in the bounded queue if necessary.

        Returns a :class:`SchedulerLease` the caller must ``release`` when the
        generation finishes, fails, or is cancelled. Raises
        :class:`SchedulerSaturated` when the queue is full or the wait times out.
        """
        # ``Semaphore.locked()`` is True when no permit is free *or* other waiters
        # are already queued (asyncio semaphores are FIFO-fair since 3.10), so this
        # is the honest "would I have to wait?" check.
        must_queue = self._sem.locked()
        if must_queue and self._waiters >= self._max_queue_depth:
            self._rejected_queue_full += 1
            raise SchedulerSaturated("queue_full", retry_after_s=self._retry_after_hint())

        wait_start = self._clock() if must_queue else 0.0
        if must_queue:
            self._waiters += 1
            self._peak_waiters = max(self._peak_waiters, self._waiters)
        try:
            if must_queue and self._queue_timeout_s > 0:
                await asyncio.wait_for(self._sem.acquire(), self._queue_timeout_s)
            else:
                # Either a slot is free (no suspension) or the operator disabled the
                # timeout (wait until a slot frees or the client disconnects).
                await self._sem.acquire()
        except TimeoutError:
            self._rejected_timeout += 1
            raise SchedulerSaturated(
                "queue_timeout", retry_after_s=self._retry_after_hint()
            ) from None
        finally:
            if must_queue:
                self._waiters -= 1

        waited = max(0.0, self._clock() - wait_start) if must_queue else 0.0
        self._in_use += 1
        self._peak_in_use = max(self._peak_in_use, self._in_use)
        self._admitted += 1
        self._wait_total += waited
        self._wait_count += 1
        self._wait_max = max(self._wait_max, waited)
        self._wait_last = waited
        return SchedulerLease(self, waited_s=waited)

    def _release(self, *, cancelled: bool) -> None:
        if self._in_use > 0:
            self._in_use -= 1
        if cancelled:
            self._cancelled += 1
        self._sem.release()

    # -- signals -----------------------------------------------------------

    def note_slow_consumer(self, waits: int = 1) -> None:
        """Record that a served request hit token-buffer backpressure.

        Emitted by the edge when a client consumed tokens slower than the model
        produced them, so the bounded token queue applied backpressure. The buffer
        stays bounded regardless; this only makes the condition observable.
        """
        if waits > 0:
            self._slow_consumer += 1

    # -- introspection -----------------------------------------------------

    def _retry_after_hint(self) -> int:
        """A small, honest backoff hint (seconds).

        Uses the observed average wait when we have samples, so a client backs off
        roughly as long as a slot actually takes to free; otherwise a 1s floor.
        """
        if self._wait_count > 0:
            avg = self._wait_total / self._wait_count
            return max(1, math.ceil(avg))
        return 1

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def queue_depth(self) -> int:
        return self._waiters

    def snapshot(self) -> dict[str, Any]:
        avg_ms = (self._wait_total / self._wait_count * 1000.0) if self._wait_count else None
        return {
            "max_concurrency": self._max_concurrency,
            "max_queue_depth": self._max_queue_depth,
            "queue_timeout_s": self._queue_timeout_s,
            "in_use": self._in_use,
            "available": self._max_concurrency - self._in_use,
            "queue_depth": self._waiters,
            "peak_in_use": self._peak_in_use,
            "peak_queue_depth": self._peak_waiters,
            "admitted_total": self._admitted,
            "rejected_total": self._rejected_queue_full + self._rejected_timeout,
            "rejected_queue_full": self._rejected_queue_full,
            "rejected_timeout": self._rejected_timeout,
            "cancelled_total": self._cancelled,
            "slow_consumer_total": self._slow_consumer,
            "wait_ms_avg": round(avg_ms, 1) if avg_ms is not None else None,
            "wait_ms_max": round(self._wait_max * 1000.0, 1),
            "wait_ms_last": round(self._wait_last * 1000.0, 1),
        }
