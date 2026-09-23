"""Virtual auto-models end to end: routing, /v1/models, dry-run (Block 10, sub-slice 5a)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.inference.registry import BackendEntry, BackendRegistry
from engine.inference.router import Router, VirtualModel
from engine.main import create_app
from tests.api.conftest import MODEL_ID
from tests.support.fake_backend import FakeBackend

LOOPBACK = ("127.0.0.1", 40000)


def _loaded(tokens: list[str]) -> FakeBackend:
    fake = FakeBackend(tokens=tokens, model_id=MODEL_ID)
    asyncio.run(fake.load())
    return fake


@pytest.fixture
def vm_client(tmp_settings: Settings) -> Iterator[TestClient]:
    primary = _loaded(["A"])
    worker = _loaded(["B"])
    app = create_app(tmp_settings, backend=primary)
    primary_entry = app.state.backend_registry.entries[0]
    worker_entry = BackendEntry(
        name="worker",
        kind="remote_vllm",
        is_local=False,
        provider=lambda: worker,
        max_in_flight=4,
    )
    registry = BackendRegistry([primary_entry, worker_entry])
    app.state.backend_registry = registry
    # A route policy that pins a virtual model to the worker only.
    app.state.router = Router(
        registry,
        [
            VirtualModel(name="worker-only", policy="route", steps=(("worker",),)),
            VirtualModel(name="tiered", policy="cascade", steps=(("primary",), ("worker",))),
        ],
    )
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def _chat(client: TestClient, model: str) -> object:
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_list_models_includes_virtual_names(vm_client: TestClient) -> None:
    ids = [m["id"] for m in vm_client.get("/v1/models").json()["data"]]
    assert ids == [MODEL_ID, "worker-only", "tiered"]


def test_virtual_route_model_pins_to_worker(vm_client: TestClient) -> None:
    resp = _chat(vm_client, "worker-only")
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "B"  # served by the worker
    assert resp.json()["model"] == "worker-only"  # response echoes the requested name


def test_base_model_still_served(vm_client: TestClient) -> None:
    resp = _chat(vm_client, MODEL_ID)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] in {"A", "B"}


def test_unknown_model_is_404(vm_client: TestClient) -> None:
    resp = _chat(vm_client, "does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


def test_route_plan_dry_run(vm_client: TestClient) -> None:
    resp = vm_client.post("/admin/route/plan", json={"model": "worker-only"})
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["policy"] == "route"
    assert plan["chosen"] == "worker"
    assert plan["steps"][0]["targets"] == ["worker"]


def test_route_plan_requires_operator(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings)  # own app -> single lifespan
    with TestClient(app, client=("203.0.113.9", 5000)) as remote:
        assert remote.post("/admin/route/plan", json={"model": "x"}).status_code == 401


def test_startup_rejects_virtual_model_with_unknown_backend(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        virtual_models=[{"name": "bad", "policy": "route", "backends": ["ghost"]}],
    )
    with pytest.raises(ValueError, match="unknown backend"):
        create_app(settings)
