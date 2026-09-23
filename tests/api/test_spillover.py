"""External-provider spillover end to end (Block 10, sub-slice 7).

Uses the real serving path with a local primary + a spillover backend registered
in the pool: local is preferred, and a request only reaches the spillover backend
when local cannot admit it. Spillover usage is metered like any backend, and the
choice is made once at admission (no mid-stream re-routing).
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


def _loaded(tokens: list[str]) -> FakeBackend:
    fake = FakeBackend(tokens=tokens, model_id=MODEL_ID)
    asyncio.run(fake.load())
    return fake


@pytest.fixture
def spill_client(tmp_path) -> Iterator[tuple[TestClient, BackendEntry]]:
    settings = Settings(data_dir=tmp_path / "data")
    local = _loaded(["L"])
    external = _loaded(["X"])
    app = create_app(settings, backend=local)
    primary_entry = app.state.backend_registry.entries[0]
    ext_entry = BackendEntry(
        name="external",
        kind="remote_vllm",
        is_local=False,
        provider=lambda: external,
        max_in_flight=4,
        spillover=True,
        external=True,
        cost_per_1k_input=1000.0,  # 1.0 / token, for metering assertions
        cost_per_1k_output=1000.0,
    )
    registry = BackendRegistry([primary_entry, ext_entry])
    app.state.backend_registry = registry
    app.state.router = Router(registry, [])
    with TestClient(app, client=LOOPBACK) as c:
        yield c, primary_entry


def _chat(client: TestClient) -> object:
    return client.post(
        "/v1/chat/completions",
        json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_local_preferred_over_spillover(spill_client) -> None:
    client, _primary = spill_client
    assert _chat(client).json()["choices"][0]["message"]["content"] == "L"


def test_spills_to_external_when_local_full_and_is_metered(spill_client) -> None:
    client, primary_entry = spill_client
    primary_entry.in_flight = primary_entry.max_in_flight  # local can't admit
    resp = _chat(client)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "X"  # served by spillover
    # Metered against the external backend.
    routes = client.get("/admin/routes").json()["routes"]
    ext_rows = [r for r in routes if r["backend"] == "external"]
    assert len(ext_rows) == 1
    assert ext_rows[0]["requests"] == 1
    assert ext_rows[0]["cost"] > 0  # external cost weights applied


def test_backends_status_shows_tier_and_external(spill_client) -> None:
    client, _primary = spill_client
    rows = client.get("/admin/backends").json()["backends"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["primary"]["tier"] == "primary"
    assert by_name["primary"]["external"] is False
    assert by_name["external"]["tier"] == "spillover"
    assert by_name["external"]["external"] is True
