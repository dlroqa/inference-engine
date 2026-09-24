"""Heterogeneous model eligibility, end to end (Block 12.1).

Builds a two-engine pool where each backend serves a *different* client-facing
model id (plus a same-model pair with different feature support), all through the
real app/serving path. Doubles as the app-level two-worker smoke required by the
slice: /v1/models lists both ids, a request for each id reaches only its engine,
/admin/backends reports each mapping, and /admin/route/plan shows only eligible
candidates including a feature constraint.
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
from tests.support.fake_backend import FakeBackend

LOOPBACK = ("127.0.0.1", 40000)


def _loaded(model_id: str, token: str, *, structured: bool = False) -> FakeBackend:
    fake = FakeBackend(tokens=[token], model_id=model_id, supports_structured_output=structured)
    asyncio.run(fake.load())
    return fake


def _entry(name: str, backend: FakeBackend, model_id: str) -> BackendEntry:
    return BackendEntry(
        name=name,
        kind="remote_vllm",
        is_local=False,
        provider=lambda: backend,
        max_in_flight=4,
        served_model=model_id,
    )


def _hetero_app(settings: Settings):
    """Primary serves model-a; a worker serves model-b (distinct ids)."""
    primary = _loaded("model-a", "A")
    worker = _loaded("model-b", "B")
    app = create_app(settings.model_copy(update={"model_id": "model-a"}), backend=primary)
    primary_entry = app.state.backend_registry.entries[0]
    primary_entry.served_model = "model-a"
    worker_entry = _entry("worker", worker, "model-b")
    app.state.backend_registry = BackendRegistry([primary_entry, worker_entry])
    app.state.router = Router(app.state.backend_registry, [])
    return app, primary_entry, worker_entry


@pytest.fixture
def hetero_client(tmp_settings: Settings) -> Iterator[TestClient]:
    app, _p, _w = _hetero_app(tmp_settings)
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def _chat(client: TestClient, model: str, **kw: object):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], **kw}
    return client.post("/v1/chat/completions", json=body)


def test_v1_models_lists_both_physical_ids(hetero_client: TestClient) -> None:
    ids = [m["id"] for m in hetero_client.get("/v1/models").json()["data"]]
    assert ids == ["model-a", "model-b"]  # sorted union across the pool


def test_request_reaches_only_its_engine(hetero_client: TestClient) -> None:
    # Distinct tokens prove which engine served the request.
    assert _chat(hetero_client, "model-a").json()["choices"][0]["message"]["content"] == "A"
    assert _chat(hetero_client, "model-b").json()["choices"][0]["message"]["content"] == "B"


def test_configured_but_down_model_is_503_unknown_is_404(hetero_client: TestClient) -> None:
    # Take model-b's only engine offline: model stays known -> 503, not 404.
    asyncio.run(hetero_client.app.state.backend_registry.entries[1].backend().unload())
    assert _chat(hetero_client, "model-b").status_code == 503
    # A model no engine serves -> 404.
    assert _chat(hetero_client, "model-z").status_code == 404


def test_admin_backends_reports_each_mapping(hetero_client: TestClient) -> None:
    rows = hetero_client.get("/admin/backends").json()["backends"]
    mapping = {r["name"]: r["model_id"] for r in rows}
    assert mapping == {"primary": "model-a", "worker": "model-b"}


def test_route_plan_shows_only_eligible_candidates(hetero_client: TestClient) -> None:
    plan = hetero_client.post("/admin/route/plan", json={"model": "model-b"}).json()
    names = {c["name"] for c in plan["steps"][0]["candidates"]}
    assert names == {"worker"}  # primary (model-a) excluded
    assert plan["chosen"] == "worker"


# -- capability-safe placement across same-model engines -------------------


@pytest.fixture
def feature_client(tmp_settings: Settings) -> Iterator[TestClient]:
    # Two engines serve model-a; only 'worker' supports structured output.
    plain = _loaded("model-a", "P", structured=False)
    smart = _loaded("model-a", "S", structured=True)
    settings = tmp_settings.model_copy(
        update={"model_id": "model-a", "allow_structured_output": True}
    )
    app = create_app(settings, backend=plain)
    primary_entry = app.state.backend_registry.entries[0]
    primary_entry.served_model = "model-a"
    worker_entry = _entry("worker", smart, "model-a")
    app.state.backend_registry = BackendRegistry([primary_entry, worker_entry])
    app.state.router = Router(app.state.backend_registry, [])
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def test_structured_output_routes_to_supporting_engine(feature_client: TestClient) -> None:
    body = {
        "model": "model-a",
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_object"},
    }
    resp = feature_client.post("/v1/chat/completions", json=body)
    assert resp.status_code == 200
    # Only the structured-capable 'worker' (token "S") may serve it, never 'primary'.
    assert resp.json()["choices"][0]["message"]["content"] == "S"


def test_route_plan_feature_constraint_narrows_candidates(feature_client: TestClient) -> None:
    plan = feature_client.post(
        "/admin/route/plan", json={"model": "model-a", "required_features": ["structured_output"]}
    ).json()
    names = {c["name"] for c in plan["steps"][0]["candidates"]}
    assert names == {"worker"}  # only the structured-capable engine is eligible
