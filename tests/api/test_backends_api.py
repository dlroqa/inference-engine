"""Multi-backend routing + status API (Block 10, sub-slice 3), end to end.

Uses the real app/serving path: a primary FakeBackend plus a second backend
registered in the pool, so selection, in-flight accounting, readiness-via-registry,
and GET /admin/backends are exercised through actual requests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.inference.registry import BackendEntry, BackendRegistry
from engine.inference.router import Router
from engine.main import create_app
from tests.api.conftest import MODEL_ID
from tests.support.fake_backend import FakeBackend

LOOPBACK = ("127.0.0.1", 40000)
REMOTE = ("203.0.113.7", 5000)


def _loaded(tokens: list[str]) -> FakeBackend:
    fake = FakeBackend(tokens=tokens, model_id=MODEL_ID)
    asyncio.run(fake.load())
    return fake


def _build_dual_app(settings: Settings):
    primary = _loaded(["A"])
    worker = _loaded(["B"])
    app = create_app(settings, backend=primary)
    # Add a second backend to the pool; primary entry (order 0) is reused.
    primary_entry = app.state.backend_registry.entries[0]
    worker_entry = BackendEntry(
        name="worker",
        kind="remote_vllm",
        is_local=False,
        provider=lambda: worker,
        max_in_flight=4,
    )
    app.state.backend_registry = BackendRegistry([primary_entry, worker_entry])
    app.state.router = Router(app.state.backend_registry, [])
    return app, worker, primary_entry


@pytest.fixture
def dual_client(tmp_settings: Settings) -> Iterator[tuple[TestClient, FakeBackend, BackendEntry]]:
    app, worker, primary_entry = _build_dual_app(tmp_settings)
    with TestClient(app, client=LOOPBACK) as c:
        yield c, worker, primary_entry


def _chat(client: TestClient, **kw: object) -> object:
    body = {"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}], **kw}
    return client.post("/v1/chat/completions", json=body)


def test_backends_status_lists_pool(dual_client) -> None:
    client, _worker, _primary_entry = dual_client
    resp = client.get("/admin/backends")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["count"] == 2
    names = [b["name"] for b in body["backends"]]
    assert names == ["primary", "worker"]
    primary, worker = body["backends"]
    assert primary["location"] == "local"
    assert worker["location"] == "remote"
    assert all(b["in_flight"] == 0 for b in body["backends"])
    # secret-free
    assert "authorization" not in resp.text.lower()


def test_backends_status_requires_operator(tmp_settings: Settings) -> None:
    app, _worker, _entry = _build_dual_app(tmp_settings)
    with TestClient(app, client=REMOTE) as remote:
        assert remote.get("/admin/backends").status_code == 401


def test_request_routes_to_least_busy_backend(dual_client) -> None:
    client, _worker, primary_entry = dual_client
    # Make the primary appear at capacity: the request must route to the worker,
    # whose distinct token proves which backend served it.
    primary_entry.in_flight = primary_entry.max_in_flight
    resp = _chat(client)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "B"


def test_in_flight_returns_to_zero_after_request(dual_client) -> None:
    client, _worker, _primary_entry = dual_client
    resp = _chat(client)
    assert resp.status_code == 200
    body = client.get("/admin/backends").json()
    assert all(b["in_flight"] == 0 for b in body["backends"])


def test_readiness_via_registry_when_primary_unloaded(dual_client) -> None:
    client, _worker, _primary_entry = dual_client
    # Unload the primary; the worker keeps the engine ready and serving.
    asyncio.run(client.app.state.backend.unload())
    models = client.get("/v1/models").json()
    assert [m["id"] for m in models["data"]] == [MODEL_ID]
    resp = _chat(client)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "B"


# -- KV/prefix-cache surfacing + prefix affinity (Block 10, sub-slice 4) ---


def _prefix_dual_app(tmp_path):
    """A pool of two prefix-cache-capable backends with affinity enabled."""
    settings = Settings(data_dir=tmp_path / "data", prefix_affinity_chars=8)
    primary = _loaded(["A"])
    worker = _loaded(["B"])
    app = create_app(settings, backend=primary)
    primary_entry = app.state.backend_registry.entries[0]
    primary_entry.prefix_cache = True  # pretend the primary is a remote with a cache
    worker_entry = BackendEntry(
        name="worker",
        kind="remote_vllm",
        is_local=False,
        provider=lambda: worker,
        max_in_flight=4,
        prefix_cache=True,
    )
    app.state.backend_registry = BackendRegistry([primary_entry, worker_entry])
    app.state.router = Router(app.state.backend_registry, [])
    return app, primary_entry, worker_entry


def test_backends_status_surfaces_cache_block(dual_client) -> None:
    client, _worker, primary_entry = dual_client
    # Populate the cached stats the background refresh would normally set.
    primary_entry.prefix_cache = True
    primary_entry.cache = {"kv_cache_utilization": 0.5, "prefix_cache_hit_rate": 0.9}
    body = client.get("/admin/backends").json()
    primary = body["backends"][0]
    assert primary["supports_prefix_cache"] is True
    assert primary["cache"] == {"kv_cache_utilization": 0.5, "prefix_cache_hit_rate": 0.9}
    # The worker had no cache scraped -> no cache block.
    assert "cache" not in body["backends"][1]


def test_prefix_affinity_routes_same_prompt_to_same_backend(tmp_path) -> None:
    app, _p, _w = _prefix_dual_app(tmp_path)
    with TestClient(app, client=LOOPBACK) as client:
        body = {"model": MODEL_ID, "messages": [{"role": "user", "content": "shared-prefix hello"}]}
        first = client.post("/v1/chat/completions", json=body).json()
        first_text = first["choices"][0]["message"]["content"]
        # The same prompt prefix must stick to the same backend across requests.
        for _ in range(4):
            again = client.post("/v1/chat/completions", json=body).json()
            assert again["choices"][0]["message"]["content"] == first_text
