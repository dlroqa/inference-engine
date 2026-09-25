"""GET /admin/routes: cost + performance attribution end to end (Block 10.5b)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app
from tests.api.conftest import MODEL_ID
from tests.support.fake_backend import FakeBackend

LOOPBACK = ("127.0.0.1", 40000)
REMOTE = ("203.0.113.7", 5000)


def _loaded() -> FakeBackend:
    fake = FakeBackend(tokens=["A", "B"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    return fake


@pytest.fixture
def priced_client(tmp_path) -> Iterator[TestClient]:
    # 1000 per 1k tokens => 1.0 per token, so cost is easy to check exactly.
    settings = Settings(
        data_dir=tmp_path / "data",
        primary_cost_per_1k_input=1000.0,
        primary_cost_per_1k_output=1000.0,
    )
    app = create_app(settings, backend=_loaded())
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def _chat(client: TestClient) -> object:
    return client.post(
        "/v1/chat/completions",
        json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_routes_records_cost_tokens_and_latency(priced_client: TestClient) -> None:
    assert _chat(priced_client).status_code == 200
    snap = priced_client.get("/admin/routes").json()
    assert len(snap["routes"]) == 1
    row = snap["routes"][0]
    assert row["model"] == MODEL_ID
    assert row["backend"] == "primary"
    assert row["requests"] == 1
    # Cost = prompt/1000*1000 + completion/1000*1000 = prompt + completion tokens.
    assert row["cost"] == pytest.approx(row["prompt_tokens"] + row["completion_tokens"])
    assert row["success_rate"] == 1.0
    assert row["avg_total_ms"] is not None  # a successful request has latency
    assert snap["totals"]["requests"] == 1


def test_routes_exposes_enriched_decision_fields(priced_client: TestClient) -> None:
    # Block 12.2a: a single-candidate physical request is a model_map on the
    # primary tier, and the snapshot surfaces the new observability fields.
    assert _chat(priced_client).status_code == 200
    row = priced_client.get("/admin/routes").json()["routes"][0]
    assert row["tier"] == "primary"
    assert row["reasons"] == {"model_map": 1}
    assert row["policies"] == {"base": 1}
    assert row["fallbacks"] == {}
    assert row["avg_queue_wait_ms"] is not None  # measured for every routed request
    # A zero-duration fake-backend response has no meaningful rate sample on
    # platforms with a coarse monotonic clock.
    assert row["avg_output_tps"] is None or row["avg_output_tps"] > 0
    assert row["upstream_attempts"] == 0  # local backend has no upstream POSTs


def test_routes_records_shed_when_backend_full(priced_client: TestClient) -> None:
    # Saturate the only backend's capacity so admission sheds the request.
    entry = priced_client.app.state.backend_registry.entries[0]
    entry.in_flight = entry.max_in_flight
    resp = _chat(priced_client)
    assert resp.status_code == 429  # retriable saturation
    snap = priced_client.get("/admin/routes").json()
    assert snap["sheds"].get(MODEL_ID) == 1
    assert snap["totals"]["sheds"] == 1


def test_routes_requires_operator(tmp_path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    with TestClient(app, client=REMOTE) as remote:
        assert remote.get("/admin/routes").status_code == 401


def test_routes_and_plan_expose_matched_workload_rule(tmp_path) -> None:
    fake = FakeBackend(tokens=["{}"], model_id=MODEL_ID, supports_structured_output=True)
    asyncio.run(fake.load())
    settings = Settings(
        data_dir=tmp_path / "data",
        model_id=MODEL_ID,
        workload_routing_enabled=True,
        workload_routing_rules=[
            {
                "name": "json-primary",
                "kind": "structured_output_preference",
                "model": MODEL_ID,
                "preferred_backends": ["primary"],
                "enabled": True,
            }
        ],
    )
    with TestClient(create_app(settings, backend=fake), client=LOOPBACK) as client:
        plan = client.post(
            "/admin/route/plan",
            json={"model": MODEL_ID, "required_features": ["structured_output"]},
        ).json()
        assert plan["workload_rule"] == "json-primary"
        assert plan["workload_rule_preferred_targets"] == ["primary"]
        assert plan["chosen"] == "primary"

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [{"role": "user", "content": "safe prompt"}],
                "response_format": {"type": "json_object"},
            },
        )
        assert response.status_code == 200
        row = client.get("/admin/routes").json()["routes"][0]
        assert row["workload_rules"] == {"json-primary": 1}
