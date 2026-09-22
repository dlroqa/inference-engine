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
