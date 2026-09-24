"""Unit tests for the backend registry + least-busy selection (Block 10, sub-slice 3)."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from engine.inference.base import GenerationStream, InferenceBackend
from engine.inference.registry import (
    BackendEntry,
    BackendRegistry,
    NoBackendAvailable,
    RouteDecision,
)
from engine.inference.types import (
    BackendState,
    Capabilities,
    FeatureUnsupportedError,
    GenerationRequest,
)


class StubBackend(InferenceBackend):
    """A minimal backend with a settable state and optional health probe."""

    def __init__(
        self,
        *,
        state: BackendState = BackendState.READY,
        model_id: str = "m",
        ctx: int = 1024,
        health: bool | Exception | None = None,
        prefix: bool = False,
        kv: bool = False,
        structured: bool = False,
    ) -> None:
        self._state = state
        self._model_id = model_id
        self._ctx = ctx
        self._health = health
        self._prefix = prefix
        self._kv = kv
        self._structured = structured

    @property
    def state(self) -> BackendState:
        return self._state

    async def load(self) -> None:  # pragma: no cover - unused
        self._state = BackendState.READY

    async def unload(self) -> None:  # pragma: no cover - unused
        self._state = BackendState.UNLOADED

    def capabilities(self) -> Capabilities:
        return Capabilities(
            backend="stub",
            model_id=self._model_id,
            context_length=self._ctx,
            supports_structured_output=self._structured,
            supports_prefix_cache=self._prefix,
            supports_kv_cache_metrics=self._kv,
        )

    async def health(self) -> dict[str, object]:  # pragma: no cover - unused
        return {"state": self._state.value}

    def generate(self, request: GenerationRequest) -> GenerationStream:  # pragma: no cover
        raise NotImplementedError

    async def check_health(self) -> bool:
        if isinstance(self._health, Exception):
            raise self._health
        return bool(self._health)


def _entry(
    name: str,
    backend: InferenceBackend,
    *,
    is_local: bool = False,
    cap: int = 1,
    prefix_cache: bool = False,
    spillover: bool = False,
    served_model: str | None = None,
) -> BackendEntry:
    return BackendEntry(
        name=name,
        kind="remote_vllm",
        is_local=is_local,
        provider=lambda: backend,
        max_in_flight=cap,
        prefix_cache=prefix_cache,
        spillover=spillover,
        served_model=served_model,
    )


def test_select_prefers_least_in_flight_then_order() -> None:
    a, b = StubBackend(), StubBackend()
    ea, eb = _entry("a", a, cap=5), _entry("b", b, cap=5)
    reg = BackendRegistry([ea, eb])
    # Tie at 0 in-flight → first by registration order.
    assert reg.select() is ea
    ea.in_flight = 2
    eb.in_flight = 1
    assert reg.select() is eb  # fewer in-flight wins


def test_select_skips_unloaded_unavailable_and_full() -> None:
    ready = StubBackend(state=BackendState.READY)
    loading = StubBackend(state=BackendState.LOADING)
    down = StubBackend(state=BackendState.READY)
    e_ready = _entry("ready", ready, cap=1)
    e_loading = _entry("loading", loading, cap=1)
    e_down = _entry("down", down, cap=1)
    e_down.available = False  # failed health probe
    reg = BackendRegistry([e_loading, e_down, e_ready])
    assert reg.select() is e_ready
    # Fill the only usable one → nothing selectable.
    e_ready.in_flight = 1
    assert reg.select() is None


def test_acquire_increments_and_lease_release_decrements() -> None:
    backend = StubBackend()
    entry = _entry("a", backend, cap=2)
    reg = BackendRegistry([entry])
    lease = reg.acquire()
    assert entry.in_flight == 1
    assert lease.backend is backend
    lease.release()
    assert entry.in_flight == 0
    lease.release()  # idempotent
    assert entry.in_flight == 0


def test_acquire_raises_busy_when_all_full() -> None:
    entry = _entry("a", StubBackend(), cap=1)
    entry.in_flight = 1
    reg = BackendRegistry([entry])
    with pytest.raises(NoBackendAvailable) as exc:
        reg.acquire()
    assert exc.value.reason == "busy"


def test_acquire_raises_unloaded_when_none_ready() -> None:
    entry = _entry("a", StubBackend(state=BackendState.UNLOADED))
    reg = BackendRegistry([entry])
    assert reg.any_ready() is False
    assert reg.representative() is None
    with pytest.raises(NoBackendAvailable) as exc:
        reg.acquire()
    assert exc.value.reason == "unloaded"


def test_representative_returns_first_ready() -> None:
    down = StubBackend(state=BackendState.UNLOADED)
    up = StubBackend(model_id="served")
    reg = BackendRegistry([_entry("down", down), _entry("up", up)])
    rep = reg.representative()
    assert rep is up
    assert rep.capabilities().model_id == "served"


def test_status_is_secret_free_and_complete() -> None:
    ready = StubBackend(model_id="served", ctx=2048)
    down = StubBackend(state=BackendState.UNLOADED)
    e_ready = _entry("local", ready, is_local=True, cap=1)
    e_ready.in_flight = 1
    reg = BackendRegistry([e_ready, _entry("remote", down)])
    rows = reg.status()
    assert [r["name"] for r in rows] == ["local", "remote"]
    assert rows[0]["location"] == "local"
    assert rows[0]["state"] == "ready"
    assert rows[0]["in_flight"] == 1
    assert rows[0]["model_id"] == "served"
    assert rows[0]["context_length"] == 2048
    assert rows[1]["location"] == "remote"
    assert rows[1]["state"] == "unloaded"
    assert rows[1]["available"] is False
    assert rows[1]["model_id"] is None
    # No secret-bearing fields leak into status.
    blob = str(rows)
    assert "api_key" not in blob and "authorization" not in blob.lower()


def test_refresh_health_local_uses_state_remote_uses_probe() -> None:
    local = StubBackend(state=BackendState.READY)
    remote_up = StubBackend(health=True)
    remote_down = StubBackend(health=False)
    remote_err = StubBackend(health=RuntimeError("boom"))
    e_local = _entry("local", local, is_local=True)
    e_up = _entry("up", remote_up)
    e_down = _entry("down", remote_down)
    e_err = _entry("err", remote_err)
    reg = BackendRegistry([e_local, e_up, e_down, e_err])

    asyncio.run(reg.refresh_health())

    assert e_local.available is True  # local: state is READY
    assert e_up.available is True  # remote probe ok
    assert e_down.available is False  # remote probe returned False
    assert e_err.available is False  # probe raised → unavailable


# -- prefix affinity (Block 10, sub-slice 4) ------------------------------


def test_affinity_same_key_routes_to_same_backend() -> None:
    a, b = StubBackend(prefix=True), StubBackend(prefix=True)
    reg = BackendRegistry(
        [_entry("a", a, cap=9, prefix_cache=True), _entry("b", b, cap=9, prefix_cache=True)]
    )
    first = reg.select(prefix_key="conversation-42")
    assert first is not None and first.prefix_cache
    # Same key is sticky regardless of subsequent calls.
    for _ in range(5):
        assert reg.select(prefix_key="conversation-42") is first


def test_affinity_spreads_across_distinct_keys() -> None:
    reg = BackendRegistry(
        [
            _entry("a", StubBackend(prefix=True), cap=99, prefix_cache=True),
            _entry("b", StubBackend(prefix=True), cap=99, prefix_cache=True),
        ]
    )
    targets = {reg.select(prefix_key=f"key-{i}").name for i in range(20)}
    assert targets == {"a", "b"}  # the hash uses both backends


def test_affinity_falls_back_to_least_busy_when_target_full() -> None:
    ea = _entry("a", StubBackend(prefix=True), cap=1, prefix_cache=True)
    eb = _entry("b", StubBackend(prefix=True), cap=1, prefix_cache=True)
    reg = BackendRegistry([ea, eb])
    key = "sticky"
    target = reg.select(prefix_key=key)
    assert target is not None
    target.in_flight = target.max_in_flight  # saturate the affine target
    fallback = reg.select(prefix_key=key)
    assert fallback is not None
    assert fallback is not target
    assert fallback.has_capacity()


def test_affinity_ignored_without_prefix_capable_backend() -> None:
    ea = _entry("a", StubBackend(), cap=5)  # prefix_cache False
    eb = _entry("b", StubBackend(), cap=5)
    reg = BackendRegistry([ea, eb])
    ea.in_flight = 3
    eb.in_flight = 1
    # No capable backend -> key is ignored, pure least-busy.
    assert reg.select(prefix_key="anything") is eb


def test_status_includes_cache_and_capability_flags() -> None:
    capable = StubBackend(prefix=True, kv=True)
    plain = StubBackend()
    e_cap = _entry("cap", capable, prefix_cache=True)
    e_cap.cache = {"kv_cache_utilization": 0.5, "prefix_cache_hit_rate": 0.9}
    reg = BackendRegistry([e_cap, _entry("plain", plain)])
    rows = reg.status()
    assert rows[0]["supports_prefix_cache"] is True
    assert rows[0]["supports_kv_cache_metrics"] is True
    assert rows[0]["cache"] == {"kv_cache_utilization": 0.5, "prefix_cache_hit_rate": 0.9}
    # Non-capable backend: flags False and no cache block.
    assert rows[1]["supports_prefix_cache"] is False
    assert rows[1]["supports_kv_cache_metrics"] is False
    assert "cache" not in rows[1]


# -- spillover tiering (Block 10, sub-slice 7) -----------------------------


def test_spillover_used_only_when_local_unavailable() -> None:
    local = StubBackend()
    ext = StubBackend()
    e_local = _entry("local", local, cap=1)
    e_ext = _entry("ext", ext, cap=5, spillover=True)
    reg = BackendRegistry([e_local, e_ext])
    # Local is ready with capacity -> always chosen over spillover.
    assert reg.select() is e_local
    # Saturate local -> spillover takes it.
    e_local.in_flight = 1
    assert reg.select() is e_ext
    # Local frees up -> back to local (spillover is overflow only).
    e_local.in_flight = 0
    assert reg.select() is e_local


def test_spillover_skipped_when_local_only_unready() -> None:
    # Local present but unloaded, spillover ready: spillover serves.
    e_local = _entry("local", StubBackend(state=BackendState.UNLOADED))
    e_ext = _entry("ext", StubBackend(), spillover=True)
    reg = BackendRegistry([e_local, e_ext])
    assert reg.select() is e_ext


def test_no_backend_when_local_and_spillover_full() -> None:
    e_local = _entry("local", StubBackend(), cap=1)
    e_ext = _entry("ext", StubBackend(), cap=1, spillover=True)
    e_local.in_flight = 1
    e_ext.in_flight = 1
    reg = BackendRegistry([e_local, e_ext])
    assert reg.select() is None
    with pytest.raises(NoBackendAvailable) as exc:
        reg.acquire()
    assert exc.value.reason == "busy"


def test_status_reports_tier_and_external() -> None:
    reg = BackendRegistry(
        [
            _entry("local", StubBackend(), is_local=True),
            _entry("ext", StubBackend(), spillover=True),
        ]
    )
    # mark external on the spillover entry directly
    reg.entries[1].external = True
    rows = reg.status()
    assert rows[0]["tier"] == "primary" and rows[0]["external"] is False
    assert rows[1]["tier"] == "spillover" and rows[1]["external"] is True


# -- heterogeneous model eligibility (Block 12.1) --------------------------


def _hetero_reg() -> BackendRegistry:
    """A pool where 'va' serves model-a and 'vb'/'sb' serve model-b."""
    return BackendRegistry(
        [
            _entry("va", StubBackend(model_id="model-a"), cap=5),
            _entry("vb", StubBackend(model_id="model-b"), cap=5),
            _entry("sb", StubBackend(model_id="model-b"), cap=5),
        ]
    )


def test_select_filters_by_model_never_picks_ineligible() -> None:
    reg = _hetero_reg()
    assert reg.select(model="model-a").name == "va"
    # model-b is served by vb (order first) and sb; least-busy within them.
    assert reg.select(model="model-b").name == "vb"
    reg.entries[1].in_flight = 3  # vb busier than sb
    assert reg.select(model="model-b").name == "sb"
    # A model no backend serves -> nothing selectable.
    assert reg.select(model="model-z") is None


def test_acquire_reason_scoped_to_model_eligible_subset() -> None:
    reg = _hetero_reg()
    # model-a's only engine (va) is full -> busy, even though model-b engines idle.
    reg.entries[0].in_flight = reg.entries[0].max_in_flight
    with pytest.raises(NoBackendAvailable) as exc:
        reg.acquire(model="model-a")
    assert exc.value.reason == "busy"
    # A known-but-unloaded model -> unloaded (not busy), scoped to that model.
    reg2 = BackendRegistry([_entry("va", StubBackend(state=BackendState.UNLOADED))])
    with pytest.raises(NoBackendAvailable) as exc2:
        reg2.acquire(model="model-a")
    assert exc2.value.reason == "unloaded"


def test_known_models_union_live_and_configured_fallback() -> None:
    live = StubBackend(model_id="model-a")
    down = StubBackend(state=BackendState.UNLOADED)
    reg = BackendRegistry(
        [
            _entry("va", live),
            _entry("vb", down, served_model="model-b"),  # unloaded but configured
        ]
    )
    # Live id from the ready backend + configured fallback from the down one.
    assert reg.known_models() == {"model-a", "model-b"}


def test_representative_for_matches_only_ready_live_model() -> None:
    reg = _hetero_reg()
    assert reg.representative_for("model-a").capabilities().model_id == "model-a"
    assert reg.representative_for("model-z") is None
    # An unloaded backend never satisfies representative_for, even if configured.
    down = BackendRegistry(
        [_entry("vb", StubBackend(state=BackendState.UNLOADED), served_model="model-b")]
    )
    assert down.representative_for("model-b") is None


def test_homogeneous_default_unchanged() -> None:
    # Two backends both serving the default id: model filter is a no-op, plain pool.
    reg = BackendRegistry(
        [
            _entry(
                "primary", StubBackend(model_id="local-model"), cap=1, served_model="local-model"
            ),
            _entry("w", StubBackend(model_id="local-model"), cap=1, served_model="local-model"),
        ]
    )
    assert reg.known_models() == {"local-model"}
    assert reg.select(model="local-model").name == "primary"
    reg.entries[0].in_flight = 1
    assert reg.select(model="local-model").name == "w"


# -- capability-safe placement (Block 12.1) --------------------------------

FEAT = frozenset({"structured_output"})


def _same_model_mixed_features() -> BackendRegistry:
    """Two engines serving model-a: only 'yes' supports structured output."""
    return BackendRegistry(
        [
            _entry("no", StubBackend(model_id="model-a", structured=False), cap=5),
            _entry("yes", StubBackend(model_id="model-a", structured=True), cap=5),
        ]
    )


def test_feature_selects_only_supporting_backend_even_if_busier() -> None:
    reg = _same_model_mixed_features()
    reg.entries[1].in_flight = 4  # 'yes' is busier, but it is the only eligible one
    assert reg.select(model="model-a", required_features=FEAT).name == "yes"
    # Without the feature, least-busy wins ('no').
    assert reg.select(model="model-a").name == "no"


def test_feature_all_supporting_full_is_busy() -> None:
    reg = _same_model_mixed_features()
    reg.entries[1].in_flight = reg.entries[1].max_in_flight  # 'yes' saturated
    with pytest.raises(NoBackendAvailable) as exc:
        reg.acquire(model="model-a", required_features=FEAT)
    assert exc.value.reason == "busy"


def test_feature_no_supporting_candidate_raises_feature_unsupported() -> None:
    reg = BackendRegistry([_entry("no", StubBackend(model_id="model-a", structured=False))])
    with pytest.raises(FeatureUnsupportedError) as exc:
        reg.acquire(model="model-a", required_features=FEAT)
    assert exc.value.features == FEAT and exc.value.model == "model-a"


def test_feature_unknown_model_is_unloaded_not_feature_error() -> None:
    reg = _same_model_mixed_features()
    with pytest.raises(NoBackendAvailable) as exc:
        reg.acquire(model="model-z", required_features=FEAT)
    assert exc.value.reason == "unloaded"


# -- route-decision attribution (Block 12.2a) ------------------------------


def test_decision_least_busy_multi_candidate() -> None:
    reg = BackendRegistry(
        [
            _entry("a", StubBackend(model_id="m"), cap=5),
            _entry("b", StubBackend(model_id="m"), cap=5),
        ]
    )
    # No model filter -> not model_map even with the pool; two candidates -> least_busy.
    lease = reg.acquire()
    d = lease.decision
    assert d is not None
    assert d.reason == "least_busy" and d.tier == "primary" and d.fallbacks == ()
    assert d.backend == "a" and d.engine == "remote_vllm"


def test_decision_model_map_single_physical_candidate() -> None:
    reg = _hetero_reg()  # va serves model-a alone; vb/sb serve model-b
    assert reg.acquire(model="model-a").decision.reason == "model_map"
    # model-b has two eligible candidates -> least_busy, not model_map.
    assert reg.acquire(model="model-b").decision.reason == "least_busy"


def test_decision_prefix_affinity_and_fallback() -> None:
    ea = _entry("a", StubBackend(prefix=True), cap=1, prefix_cache=True)
    eb = _entry("b", StubBackend(prefix=True), cap=1, prefix_cache=True)
    reg = BackendRegistry([ea, eb])
    first = reg.acquire(prefix_key="sticky")
    assert first.decision.reason == "prefix_affinity"
    target = first.decision.backend
    # Saturate the affine target; the next same-key request falls back to least-busy.
    second = reg.acquire(prefix_key="sticky")
    assert second.decision.reason == "affinity_fallback"
    assert second.decision.backend != target


def test_decision_spillover_tier_and_fallback() -> None:
    e_local = _entry("local", StubBackend(model_id="m"), cap=1)
    e_ext = _entry("ext", StubBackend(model_id="m"), cap=5, spillover=True)
    reg = BackendRegistry([e_local, e_ext])
    e_local.in_flight = e_local.max_in_flight  # force spillover
    d = reg.acquire().decision
    assert d.backend == "ext" and d.tier == "spillover"
    assert d.fallbacks == ("spillover",)


def test_route_decision_is_frozen() -> None:
    d = RouteDecision(backend="a", engine="remote_vllm", tier="primary", reason="least_busy")
    with pytest.raises(FrozenInstanceError):
        d.reason = "model_map"  # type: ignore[misc]
