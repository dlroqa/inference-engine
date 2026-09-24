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
from typing import Literal

from engine.inference.base import InferenceBackend
from engine.inference.types import BackendState, FeatureUnsupportedError
from engine.logging_setup import get_logger

_log = get_logger("engine.inference.registry")

_READYISH = (BackendState.READY, BackendState.GENERATING)

#: Request-feature name -> the ``Capabilities`` attribute that gates it (Block
#: 12.1). A feature is eligible on a backend only when this attribute is true on
#: its *live* capabilities. Extend as feature-aware routing grows.
_FEATURE_ATTR = {"structured_output": "supports_structured_output"}


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
    #: Token cost weights for per-route cost accounting (Block 10.5b); 0 = free
    #: (e.g. a local backend). Cost = prompt/1000*in + completion/1000*out.
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    #: Spillover tier (Block 10.7): a spillover backend (e.g. an external
    #: provider) is used only when no non-spillover backend can admit the
    #: request. ``external`` marks off-premise providers (informational/status).
    spillover: bool = False
    external: bool = False
    #: Configured client-facing model id (Block 12.1). Used *only* as a fallback in
    #: :meth:`current_model` while the backend is unloaded/unhealthy, so a
    #: configured model stays "known" (→ 503) rather than looking unknown (→ 404).
    #: Live placement always uses the backend's own ``capabilities().model_id``.
    served_model: str | None = None

    def backend(self) -> InferenceBackend | None:
        return self.provider()

    def is_loaded(self) -> bool:
        backend = self.provider()
        return backend is not None and backend.state in _READYISH

    def is_ready(self) -> bool:
        return self.available and self.is_loaded()

    def has_capacity(self) -> bool:
        return self.in_flight < self.max_in_flight

    def live_model(self) -> str | None:
        """The model this backend serves *right now*, or None if not ready.

        The placement source of truth: read from live capabilities only when the
        entry is ready (health-probe included), so a failed remote is excluded.
        """
        if self.is_ready():
            backend = self.backend()
            if backend is not None:
                return backend.capabilities().model_id
        return None

    def current_model(self) -> str | None:
        """Live model when ready, else the configured ``served_model`` fallback."""
        live = self.live_model()
        return live if live is not None else self.served_model


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Why a request landed on a backend (Block 12.2a) — observability only.

    ``backend`` is the configured entry name (the current pool identifier reported
    to operators); ``engine`` is its kind (vLLM/SGLang/local/external). ``reason``
    names the algorithm that selected the final entry; ``fallbacks`` records extra
    routing facts without overwriting ``reason`` (deterministic order: cascade
    escalation first, spillover second). Frozen — rebuild with ``dataclasses.replace``
    to add router policy context; never mutate a decision attached to a lease.
    """

    backend: str
    engine: str
    tier: Literal["primary", "spillover"]
    reason: Literal["model_map", "least_busy", "prefix_affinity", "affinity_fallback"]
    fallbacks: tuple[Literal["cascade_escalation", "spillover"], ...] = ()
    policy: Literal["base", "route", "cascade"] = "base"
    step: int | None = None


@dataclass(slots=True)
class RegistryLease:
    """Holds one in-flight slot on a chosen backend; release exactly once."""

    registry: BackendRegistry
    entry: BackendEntry
    _released: bool = field(default=False)
    #: Why this lease's backend was chosen (Block 12.2a). Always set for a
    #: successfully acquired lease; the router may replace it with policy context.
    decision: RouteDecision | None = None

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
        """A ready backend to read capabilities from (any model; diagnostic only)."""
        for entry in self._entries:
            if entry.is_ready():
                backend = entry.backend()
                if backend is not None:
                    return backend
        return None

    def representative_for(self, model: str) -> InferenceBackend | None:
        """The first ready backend whose *live* model id matches (Block 12.1).

        Convenience/diagnostic only — it is not a licence to validate a request
        against a different backend from the one that will be leased; capability
        eligibility is enforced in :meth:`select`/:meth:`acquire`.
        """
        for entry in self._entries:
            if entry.live_model() == model:
                backend = entry.backend()
                if backend is not None:
                    return backend
        return None

    def known_models(self) -> set[str]:
        """Every client-facing model id the pool serves or is configured to serve.

        Union of live ready ids and configured ``served_model`` fallbacks, so a
        configured-but-currently-down model stays known (→ 503, not 404).
        """
        return {m for e in self._entries if (m := e.current_model()) is not None}

    def eligible(
        self,
        entry: BackendEntry,
        model: str | None,
        required_features: frozenset[str],
    ) -> bool:
        """Whether ``entry`` may serve a request for ``model`` with these features.

        Placement eligibility only — uses the entry's *live* model/capabilities, so
        an unready or wrong-model backend is never eligible. Readiness/capacity are
        still applied separately by :meth:`_pick`.
        """
        if model is not None and entry.live_model() != model:
            return False
        if required_features:
            backend = entry.backend() if entry.is_ready() else None
            if backend is None:
                return False
            caps = backend.capabilities()
            if not all(getattr(caps, _FEATURE_ATTR[f], False) for f in required_features):
                return False
        return True

    def select(
        self,
        prefix_key: str | None = None,
        allowed: frozenset[str] | None = None,
        model: str | None = None,
        required_features: frozenset[str] = frozenset(),
    ) -> BackendEntry | None:
        """Pick a backend: prefix-affinity first (when applicable), else least-busy.

        ``allowed`` restricts the candidates to those backend names (used by the
        router to scope a virtual model's route/cascade step); None = whole pool.
        ``model`` restricts to backends whose *live* model id matches (Block 12.1),
        and ``required_features`` to backends whose live capabilities support every
        named feature. Both filters are applied *before* spillover tiers, prefix
        affinity, and least-busy selection, so an ineligible engine is never chosen.

        With a ``prefix_key`` and at least one ready prefix-cache-capable backend, a
        consistent hash of the key maps the request to one such backend so its
        prefix cache is reused. If that target is at capacity we fall back to
        least-busy (never overload for affinity's sake). Otherwise — no key, no
        capable backend, or an unrelated pool — it is plain least-busy among ready,
        not-full backends (identical to sub-slice 3).
        """
        return self._select_detailed(prefix_key, allowed, model, required_features)[0]

    def _select_detailed(
        self,
        prefix_key: str | None,
        allowed: frozenset[str] | None,
        model: str | None,
        required_features: frozenset[str],
    ) -> tuple[BackendEntry | None, str, str, int]:
        """``select`` plus attribution: (entry, reason, tier, available_in_tier).

        ``available_in_tier`` is the count of ready, eligible, capacity-available
        candidates in the *selected* tier before selection; it lets ``acquire``
        label a single-candidate physical placement ``model_map`` (Block 12.2a).
        Selection behavior is identical to Block 10/12.1 — this only names it.
        """
        pool = [
            e
            for e in self._entries
            if (allowed is None or e.name in allowed) and self.eligible(e, model, required_features)
        ]
        # Two-tier (Block 10.7): prefer non-spillover (local) backends; only use
        # spillover (e.g. external providers) when no local backend can admit.
        local = [e for e in pool if not e.spillover]
        spill = [e for e in pool if e.spillover]
        entry, reason = self._pick(local, prefix_key)
        tier = "primary"
        if entry is None:
            entry, reason = self._pick(spill, prefix_key)
            tier = "spillover"
        if entry is None:
            return None, "", tier, 0
        tier_pool = local if tier == "primary" else spill
        available = sum(1 for e in tier_pool if e.is_ready() and e.has_capacity())
        # A physical-model request with a single capacity-available candidate is a
        # deterministic model map, not a least-busy choice among peers.
        if reason == "least_busy" and available == 1 and model is not None:
            reason = "model_map"
        return entry, reason, tier, available

    def _pick(
        self, pool: list[BackendEntry], prefix_key: str | None
    ) -> tuple[BackendEntry | None, str]:
        candidates = [e for e in pool if e.is_ready() and e.has_capacity()]
        if prefix_key:
            capable = sorted(
                (e for e in pool if e.is_ready() and e.prefix_cache),
                key=lambda e: e.order,
            )
            if capable:
                digest = hashlib.sha256(prefix_key.encode("utf-8")).hexdigest()
                target = capable[int(digest, 16) % len(capable)]
                if target.has_capacity():
                    return target, "prefix_affinity"
                # target saturated: fall through to least-busy placement
                if candidates:
                    fallback = min(candidates, key=lambda e: (e.in_flight, e.order))
                    return fallback, "affinity_fallback"
                return None, ""
        if not candidates:
            return None, ""
        return min(candidates, key=lambda e: (e.in_flight, e.order)), "least_busy"

    def acquire(
        self,
        prefix_key: str | None = None,
        allowed: frozenset[str] | None = None,
        model: str | None = None,
        required_features: frozenset[str] = frozenset(),
    ) -> RegistryLease:
        """Reserve an in-flight slot on the selected backend, or raise.

        Binds capability validation to placement (Block 12.1): the same call that
        reserves the lease also decides feature eligibility, so there is no
        time-of-check/time-of-placement gap. When nothing can serve the request it
        raises, distinguishing three cases over the model-eligible subset:

        * a feature-capable backend is ready but full -> ``NoBackendAvailable("busy")``
          (retriable saturation);
        * the model has a ready backend but none support a required feature ->
          :class:`FeatureUnsupportedError` (a 400 request error);
        * no ready backend serves the model at all -> ``NoBackendAvailable("unloaded")``.
        """
        entry, reason, tier, _available = self._select_detailed(
            prefix_key, allowed, model, required_features
        )
        if entry is None:
            scope = [e for e in self._entries if allowed is None or e.name in allowed]
            # Ready backends that serve the requested model (ignoring features).
            model_ready = [
                e for e in scope if e.is_ready() and (model is None or e.live_model() == model)
            ]
            if not model_ready:
                raise NoBackendAvailable("unloaded")
            feature_ready = [e for e in model_ready if self.eligible(e, model, required_features)]
            if not feature_ready:
                raise FeatureUnsupportedError(required_features, model=model)
            # Feature-capable backends exist but all are at capacity.
            raise NoBackendAvailable("busy")
        entry.in_flight += 1
        decision = RouteDecision(
            backend=entry.name,
            engine=entry.kind,
            tier=tier,  # type: ignore[arg-type]
            reason=reason,  # type: ignore[arg-type]
            fallbacks=("spillover",) if tier == "spillover" else (),
        )
        return RegistryLease(registry=self, entry=entry, decision=decision)

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
                "served_model": entry.served_model,
                "context_length": context_length,
                "supports_prefix_cache": entry.prefix_cache,
                "supports_kv_cache_metrics": supports_kv_metrics,
                "tier": "spillover" if entry.spillover else "primary",
                "external": entry.external,
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
