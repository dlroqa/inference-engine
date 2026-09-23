"""Backend registry + health-aware least-busy routing (Block 10, sub-slice 3).

Blocks 1-10.2 served every request from a single backend (``app.state.backend``).
This module lets the engine hold **several** backends at once — a local
llama.cpp plus remote vLLM/SGLang workers, or several remote workers — and route
each admitted request to the **least-busy healthy** one. A single-backend setup
is just a registry of one, so nothing changes for it.

The engine still presents one ``model_id`` to clients; the registry only decides
which worker serves a given request. Workers are assumed homogeneous (same model
/ capabilities); heterogeneous pools, routing policies, and virtual auto-models
are later sub-slices. The global Block-7 scheduler stays the overall admission
cap; the registry does *placement* and per-backend capacity within it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field

from engine.inference.base import InferenceBackend
from engine.inference.types import BackendState
from engine.logging_setup import get_logger

_log = get_logger("engine.inference.registry")

_READYISH = (BackendState.READY, BackendState.GENERATING)


@dataclass
class BackendEntry:
    """One backend in the registry, with live in-flight + health accounting.

    ``provider`` returns the current backend object rather than a fixed reference
    so the primary entry tracks ``app.state.backend`` across admin load/unload
    swaps; remote workers use a constant provider.
    """

    name: str
    kind: str
    is_local: bool
    provider: Callable[[], InferenceBackend | None]
    max_in_flight: int = 1
    #: Whether this backend maintains a prefix cache worth affinity routing
    #: (vLLM/SGLang yes; local llama.cpp no). Set at construction, honestly.
    prefix_cache: bool = False
    order: int = 0
    in_flight: int = 0
    #: Liveness flag maintained by :meth:`BackendRegistry.refresh_health`; local
    #: backends rely on state, remotes on a lightweight probe. Defaults True so a
    #: never-probed backend is usable as long as it is loaded.
    available: bool = True
    #: Last-scraped KV/prefix-cache stats (Block 10.4), or None if the backend
    #: does not export them. Refreshed by :meth:`BackendRegistry.refresh_health`.
    cache: dict[str, float] | None = None

    def backend(self) -> InferenceBackend | None:
        return self.provider()

    def is_loaded(self) -> bool:
        backend = self.provider()
        return backend is not None and backend.state in _READYISH

    def is_ready(self) -> bool:
        return self.available and self.is_loaded()

    def has_capacity(self) -> bool:
        return self.in_flight < self.max_in_flight


@dataclass(slots=True)
class RegistryLease:
    """Holds one in-flight slot on a chosen backend; release exactly once."""

    registry: BackendRegistry
    entry: BackendEntry
    _released: bool = field(default=False)

    @property
    def backend(self) -> InferenceBackend:
        backend = self.entry.backend()
        assert backend is not None  # guaranteed by select()
        return backend

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.registry._release(self.entry)


class NoBackendAvailable(Exception):
    """Raised by :meth:`BackendRegistry.acquire` when no backend can take work.

    ``reason`` is ``"unloaded"`` (nothing is loaded/healthy) or ``"busy"`` (all
    healthy backends are at capacity) so the edge can map it to the right status
    (503 vs retriable saturation).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BackendRegistry:
    """A set of backends with least-busy, health-aware selection."""

    def __init__(self, entries: list[BackendEntry]) -> None:
        self._entries = entries
        for i, entry in enumerate(entries):
            entry.order = i

    @property
    def entries(self) -> list[BackendEntry]:
        return list(self._entries)

    def any_ready(self) -> bool:
        return any(e.is_ready() for e in self._entries)

    def representative(self) -> InferenceBackend | None:
        """A ready backend to read capabilities/model_id from (pool is homogeneous)."""
        for entry in self._entries:
            if entry.is_ready():
                backend = entry.backend()
                if backend is not None:
                    return backend
        return None

    def select(self, prefix_key: str | None = None) -> BackendEntry | None:
        """Pick a backend: prefix-affinity first (when applicable), else least-busy.

        With a ``prefix_key`` and at least one ready prefix-cache-capable backend, a
        consistent hash of the key maps the request to one such backend so its
        prefix cache is reused. If that target is at capacity we fall back to
        least-busy (never overload for affinity's sake). Otherwise — no key, no
        capable backend, or an unrelated pool — it is plain least-busy among ready,
        not-full backends (identical to sub-slice 3).
        """
        candidates = [e for e in self._entries if e.is_ready() and e.has_capacity()]
        if prefix_key:
            capable = sorted(
                (e for e in self._entries if e.is_ready() and e.prefix_cache),
                key=lambda e: e.order,
            )
            if capable:
                digest = hashlib.sha256(prefix_key.encode("utf-8")).hexdigest()
                target = capable[int(digest, 16) % len(capable)]
                if target.has_capacity():
                    return target
                # target saturated: fall through to least-busy placement
        if not candidates:
            return None
        return min(candidates, key=lambda e: (e.in_flight, e.order))

    def acquire(self, prefix_key: str | None = None) -> RegistryLease:
        """Reserve an in-flight slot on the selected backend, or raise."""
        entry = self.select(prefix_key)
        if entry is None:
            # Distinguish "nothing loaded/healthy" from "all healthy ones full".
            reason = "busy" if any(e.is_ready() for e in self._entries) else "unloaded"
            raise NoBackendAvailable(reason)
        entry.in_flight += 1
        return RegistryLease(registry=self, entry=entry)

    def _release(self, entry: BackendEntry) -> None:
        if entry.in_flight > 0:
            entry.in_flight -= 1

    def status(self) -> list[dict[str, object]]:
        """Operator-facing snapshot of every backend (secret-free)."""
        snapshot: list[dict[str, object]] = []
        for entry in self._entries:
            backend = entry.backend()
            state = backend.state.value if backend is not None else "unloaded"
            context_length: int | None = None
            model_id: str | None = None
            if backend is not None and backend.state in _READYISH:
                caps = backend.capabilities()
                context_length = caps.context_length
                model_id = caps.model_id
            supports_kv_metrics = False
            if backend is not None and backend.state in _READYISH:
                supports_kv_metrics = backend.capabilities().supports_kv_cache_metrics
            row: dict[str, object] = {
                "name": entry.name,
                "kind": entry.kind,
                "location": "local" if entry.is_local else "remote",
                "state": state,
                "available": entry.is_ready(),
                "in_flight": entry.in_flight,
                "max_in_flight": entry.max_in_flight,
                "model_id": model_id,
                "context_length": context_length,
                "supports_prefix_cache": entry.prefix_cache,
                "supports_kv_cache_metrics": supports_kv_metrics,
            }
            if entry.cache is not None:
                row["cache"] = entry.cache
            snapshot.append(row)
        return snapshot

    async def refresh_health(self) -> None:
        """Update each entry's ``available`` flag (local: state; remote: probe)."""
        for entry in self._entries:
            backend = entry.backend()
            if backend is None:
                entry.available = False
                continue
            checker = getattr(backend, "check_health", None)
            if entry.is_local or checker is None:
                entry.available = backend.state in _READYISH
                continue
            try:
                ok = await checker()
            except Exception:  # pragma: no cover - defensive
                ok = False
            if ok != entry.available:
                _log.info(
                    "backend_health_change",
                    extra={"backend": entry.name, "available": ok},
                )
            entry.available = ok
            stats = getattr(backend, "cache_stats", None)
            if ok and callable(stats):
                try:
                    entry.cache = await stats()
                except Exception:  # pragma: no cover - defensive
                    entry.cache = None
