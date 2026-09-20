"""Cumulative and rolling request/token counters.

These are the engine-wide operability counters surfaced on the metrics snapshot:
total and active requests, error count, prompt/completion tokens, uptime, and a
rolling requests-per-minute figure. They are updated from request handlers (which
run on the event loop) and read by the sampler and REST snapshot; a lock keeps
them correct even if a handler finalizes from a worker thread.

Per-key/per-client accounting is the usage-events store's job (Block 3); this is
the low-cardinality, whole-engine view.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(slots=True)
class CountersSnapshot:
    requests_total: int
    requests_active: int
    requests_errors: int
    prompt_tokens_total: int
    completion_tokens_total: int
    requests_per_min: int
    uptime_s: float


class Counters:
    """Thread-safe engine-wide counters."""

    def __init__(self, *, now: float | None = None) -> None:
        self._lock = threading.Lock()
        self._started_at = now if now is not None else time.time()
        self._requests_total = 0
        self._requests_active = 0
        self._requests_errors = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        # Timestamps of recent request starts, for a rolling requests/min figure.
        self._recent_starts: deque[float] = deque()

    def request_started(self, now: float | None = None) -> None:
        ts = now if now is not None else time.time()
        with self._lock:
            self._requests_total += 1
            self._requests_active += 1
            self._recent_starts.append(ts)
            self._trim(ts)

    def request_finished(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        error: bool = False,
    ) -> None:
        with self._lock:
            if self._requests_active > 0:
                self._requests_active -= 1
            self._prompt_tokens += max(0, prompt_tokens)
            self._completion_tokens += max(0, completion_tokens)
            if error:
                self._requests_errors += 1

    def _trim(self, now: float) -> None:
        cutoff = now - 60.0
        while self._recent_starts and self._recent_starts[0] < cutoff:
            self._recent_starts.popleft()

    def snapshot(self, now: float | None = None) -> CountersSnapshot:
        ts = now if now is not None else time.time()
        with self._lock:
            self._trim(ts)
            return CountersSnapshot(
                requests_total=self._requests_total,
                requests_active=self._requests_active,
                requests_errors=self._requests_errors,
                prompt_tokens_total=self._prompt_tokens,
                completion_tokens_total=self._completion_tokens,
                requests_per_min=len(self._recent_starts),
                uptime_s=max(0.0, ts - self._started_at),
            )
