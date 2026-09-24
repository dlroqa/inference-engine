"""Unit tests for virtual auto-models + route/cascade routing (Block 10, sub-slice 5a)."""

from __future__ import annotations

import pytest

from engine.inference.registry import BackendEntry, BackendRegistry, NoBackendAvailable
from engine.inference.router import Router, VirtualModel
from engine.inference.types import BackendState, FeatureUnsupportedError
from tests.inference.test_registry import StubBackend


def _entry(
    name: str, backend: StubBackend, *, cap: int = 5, served_model: str | None = None
) -> BackendEntry:
    return BackendEntry(
        name=name,
        kind="remote_vllm",
        is_local=False,
        provider=lambda: backend,
        max_in_flight=cap,
        served_model=served_model,
    )


def _registry() -> BackendRegistry:
    # First entry's model_id is the base served id the router reports.
    return BackendRegistry(
        [
            _entry("primary", StubBackend(model_id="base")),
            _entry("b", StubBackend(model_id="base")),
            _entry("c", StubBackend(model_id="base")),
        ]
    )


def _router(registry: BackendRegistry) -> Router:
    return Router(
        registry,
        [
            VirtualModel(name="only-b", policy="route", steps=(("b",),)),
            VirtualModel(name="tiered", policy="cascade", steps=(("primary",), ("b", "c"))),
        ],
    )


def test_known_model_and_model_names() -> None:
    router = _router(_registry())
    assert router.known_model("base") is True  # base served id
    assert router.known_model("only-b") is True
    assert router.known_model("tiered") is True
    assert router.known_model("nope") is False
    assert router.model_names() == ["base", "only-b", "tiered"]


def test_route_policy_restricts_to_allowed_backend() -> None:
    router = _router(_registry())
    for _ in range(5):
        lease = router.acquire("only-b")
        assert lease.entry.name == "b"  # route policy pins to the single allowed backend
        lease.release()


def test_base_model_uses_whole_pool_least_busy() -> None:
    reg = _registry()
    router = _router(reg)
    reg.entries[0].in_flight = 2  # primary busy
    reg.entries[1].in_flight = 1  # b
    lease = router.acquire("base")
    assert lease.entry.name == "c"  # least busy across the whole pool


def test_cascade_falls_back_to_next_step() -> None:
    reg = _registry()
    router = _router(reg)
    # First step targets "primary"; make it unavailable so the cascade escalates.
    reg.entries[0].provider = lambda: StubBackend(state=BackendState.UNLOADED)
    lease = router.acquire("tiered")
    assert lease.entry.name in {"b", "c"}  # served by the second step


def test_cascade_raises_when_all_steps_exhausted() -> None:
    reg = _registry()
    router = _router(reg)
    for e in reg.entries:
        e.provider = lambda: StubBackend(state=BackendState.UNLOADED)
    with pytest.raises(NoBackendAvailable):
        router.acquire("tiered")


def test_plan_is_dry_run_and_explains_choice() -> None:
    reg = _registry()
    router = _router(reg)
    before = [e.in_flight for e in reg.entries]
    plan = router.plan("only-b")
    assert plan["model"] == "only-b"
    assert plan["policy"] == "route"
    assert plan["chosen"] == "b"
    assert plan["steps"][0]["targets"] == ["b"]
    # Dry-run must not reserve anything.
    assert [e.in_flight for e in reg.entries] == before


def test_plan_shows_cascade_step_fallback() -> None:
    reg = _registry()
    router = _router(reg)
    reg.entries[0].provider = lambda: StubBackend(state=BackendState.UNLOADED)
    plan = router.plan("tiered")
    assert plan["policy"] == "cascade"
    assert plan["chosen"] in {"b", "c"}
    assert plan["chosen_step"] == 1  # first step (primary) was unavailable
    assert plan["steps"][0]["chosen"] is None


# -- heterogeneous physical models (Block 12.1) ----------------------------


def _hetero_router() -> tuple[BackendRegistry, Router]:
    reg = BackendRegistry(
        [
            _entry("va", StubBackend(model_id="model-a")),
            _entry("vb", StubBackend(model_id="model-b")),
            _entry("sb", StubBackend(model_id="model-b")),
        ]
    )
    router = Router(reg, [VirtualModel(name="ab", policy="route", steps=(("va", "vb"),))])
    return reg, router


def test_physical_model_names_are_union_plus_virtual() -> None:
    _reg, router = _hetero_router()
    assert router.model_names() == ["model-a", "model-b", "ab"]
    assert router.known_model("model-a") and router.known_model("model-b")
    assert router.known_model("ab") and not router.known_model("model-z")


def test_physical_acquire_reaches_only_eligible_engine() -> None:
    _reg, router = _hetero_router()
    lease = router.acquire("model-a")
    assert lease.entry.name == "va"  # only engine serving model-a
    lease.release()
    for _ in range(4):
        lease = router.acquire("model-b")
        assert lease.entry.name in {"vb", "sb"}  # never va
        lease.release()


def test_plan_lists_only_eligible_candidates() -> None:
    _reg, router = _hetero_router()
    plan = router.plan("model-b")
    names = {c["name"] for c in plan["steps"][0]["candidates"]}
    assert names == {"vb", "sb"}  # va (model-a) excluded
    assert plan["chosen"] in {"vb", "sb"}


def test_virtual_route_applies_feature_eligibility() -> None:
    # Virtual 'ab' spans two engines serving model-a; only 'yes' supports structured.
    reg = BackendRegistry(
        [
            _entry("no", StubBackend(model_id="model-a", structured=False)),
            _entry("yes", StubBackend(model_id="model-a", structured=True)),
        ]
    )
    router = Router(reg, [VirtualModel(name="ab", policy="route", steps=(("no", "yes"),))])
    feat = frozenset({"structured_output"})
    for _ in range(3):
        lease = router.acquire("ab", required_features=feat)
        assert lease.entry.name == "yes"  # feature narrows the virtual route
        lease.release()
    # No supporting backend at all -> the virtual route reports the feature error.
    reg2 = BackendRegistry([_entry("no", StubBackend(model_id="model-a", structured=False))])
    router2 = Router(reg2, [VirtualModel(name="ab", policy="route", steps=(("no",),))])
    with pytest.raises(FeatureUnsupportedError):
        router2.acquire("ab", required_features=feat)
