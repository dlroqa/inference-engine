"""Unit tests for virtual auto-models + route/cascade routing (Block 10, sub-slice 5a)."""

from __future__ import annotations

import pytest

from engine.inference.registry import BackendEntry, BackendRegistry, NoBackendAvailable
from engine.inference.router import Router, VirtualModel
from engine.inference.types import BackendState
from tests.inference.test_registry import StubBackend


def _entry(name: str, backend: StubBackend, *, cap: int = 5) -> BackendEntry:
    return BackendEntry(
        name=name,
        kind="remote_vllm",
        is_local=False,
        provider=lambda: backend,
        max_in_flight=cap,
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
