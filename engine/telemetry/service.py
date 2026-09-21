"""The telemetry service: snapshot builder, metrics sampler, and feed helpers.

One :class:`Telemetry` instance lives on ``app.state`` and ties the pieces
together: the event bus, engine counters, the resource sampler, and the power
probe. It builds the metrics snapshot (shared by ``/ws/metrics`` and the REST
``/metrics`` fallback), runs the periodic sampler task, and offers small helpers
so request handlers publish well-formed live-feed events without hand-building
dicts.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from engine.inference.base import InferenceBackend
from engine.inference.types import BackendState
from engine.telemetry.counters import Counters
from engine.telemetry.events import Event, EventBus, EventChannel, EventType
from engine.telemetry.power import PowerProbe, energy_state
from engine.telemetry.resources import ResourceSampler, gpu


class Telemetry:
    def __init__(
        self,
        *,
        bus: EventBus,
        counters: Counters,
        data_dir: Path | None = None,
        sample_interval_s: float = 1.0,
    ) -> None:
        self.bus = bus
        self.counters = counters
        self.resources = ResourceSampler(disk_path=data_dir)
        self.power = PowerProbe()
        self.sample_interval_s = sample_interval_s
        # Optional admission scheduler (Block 7); its metrics ride the snapshot.
        self.scheduler: Any | None = None
        self._task: asyncio.Task[None] | None = None
        # For measured completion-token throughput between samples.
        self._last_completion_total: int | None = None
        self._last_sample_ts: float | None = None

    # -- snapshot ----------------------------------------------------------

    def _backend_panel(self, backend: InferenceBackend | None) -> dict[str, Any]:
        if backend is None:
            return {"state": BackendState.UNLOADED.value, "model_id": None, "available": False}
        state = backend.state
        available = state in (BackendState.READY, BackendState.GENERATING)
        model_id = backend.capabilities().model_id if available else None
        return {"state": state.value, "model_id": model_id, "available": available}

    def _throughput_tok_per_s(self, completion_total: int, now: float) -> float | None:
        prev_total = self._last_completion_total
        prev_ts = self._last_sample_ts
        self._last_completion_total = completion_total
        self._last_sample_ts = now
        if prev_total is None or prev_ts is None:
            return None
        dt = now - prev_ts
        if dt <= 0:
            return None
        produced = completion_total - prev_total
        if produced <= 0:
            return 0.0
        return produced / dt

    def build_snapshot(
        self, backend: InferenceBackend | None, now: float | None = None
    ) -> dict[str, Any]:
        now = now if now is not None else time.time()
        counters = self.counters.snapshot(now)
        tok_per_s = self._throughput_tok_per_s(counters.completion_tokens_total, now)
        reading = self.power.sample()
        return {
            "ts": now,
            "uptime_s": round(counters.uptime_s, 3),
            "counters": {
                "requests_total": counters.requests_total,
                "requests_active": counters.requests_active,
                "requests_errors": counters.requests_errors,
                "prompt_tokens_total": counters.prompt_tokens_total,
                "completion_tokens_total": counters.completion_tokens_total,
                "requests_per_min": counters.requests_per_min,
            },
            "throughput": {"completion_tokens_per_s": tok_per_s},
            "resources": self.resources.snapshot(),
            "gpu": gpu(),
            "energy": energy_state(reading, tok_per_s),
            "backend": self._backend_panel(backend),
            "scheduler": self.scheduler.snapshot() if self.scheduler is not None else None,
        }

    def publish_snapshot(self, backend: InferenceBackend | None) -> dict[str, Any]:
        snapshot = self.build_snapshot(backend)
        self.bus.publish(EventChannel.METRICS, Event(EventType.METRICS, snapshot))
        return snapshot

    # -- live feed helpers -------------------------------------------------

    def feed(self, event_type: EventType, data: dict[str, Any]) -> None:
        self.bus.publish(EventChannel.FEED, Event(event_type, data))

    def request_start(
        self, *, request_id: str, endpoint: str, model: str | None, key_id: str
    ) -> None:
        self.feed(
            EventType.REQUEST_START,
            {"request_id": request_id, "endpoint": endpoint, "model": model, "key_id": key_id},
        )

    def request_progress(self, *, request_id: str, tokens: int) -> None:
        self.feed(EventType.REQUEST_PROGRESS, {"request_id": request_id, "tokens": tokens})

    def request_end(
        self,
        *,
        request_id: str,
        model: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        finish_reason: str,
        ttft_ms: float | None,
        total_ms: float | None,
    ) -> None:
        self.feed(
            EventType.REQUEST_END,
            {
                "request_id": request_id,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "finish_reason": finish_reason,
                "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
                "total_ms": round(total_ms, 1) if total_ms is not None else None,
            },
        )

    def request_error(
        self, *, request_id: str, category: str, endpoint: str, model: str | None
    ) -> None:
        self.feed(
            EventType.REQUEST_ERROR,
            {
                "request_id": request_id,
                "category": category,
                "endpoint": endpoint,
                "model": model,
            },
        )

    def request_rejected(
        self, *, request_id: str, endpoint: str, reason: str, retry_after_s: int
    ) -> None:
        self.feed(
            EventType.REQUEST_REJECTED,
            {
                "request_id": request_id,
                "endpoint": endpoint,
                "reason": reason,
                "retry_after_s": retry_after_s,
            },
        )

    # -- sampler task ------------------------------------------------------

    async def _run(self, get_backend: Callable[[], InferenceBackend | None]) -> None:
        while True:
            try:
                self.publish_snapshot(get_backend())
            except Exception:  # pragma: no cover - sampler must not crash the loop
                pass
            await asyncio.sleep(self.sample_interval_s)

    def start(self, get_backend: Callable[[], InferenceBackend | None]) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(get_backend))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
