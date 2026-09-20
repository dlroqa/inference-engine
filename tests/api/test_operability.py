"""Operability surface: metrics/feed WebSockets, REST ops, and access gating.

WebSocket happy paths run against a real Uvicorn server (the in-process ASGI test
transport does not model connection teardown cleanly for long-lived sockets);
HTTP endpoints and the fast rejection path use the lighter TestClient. The gate
logic itself is unit-tested directly.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.sync.client import connect as ws_connect

from engine.auth.keys import KeyStore
from engine.config import Settings
from engine.gateway import Gateway
from engine.main import create_app
from engine.quota.store import UsageStore
from tests.api.conftest import MODEL_ID, running_app
from tests.support.fake_backend import FakeBackend

REMOTE = ("203.0.113.7", 5000)
_CHAT = {"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]}


def _loaded_fake(**kwargs: object) -> FakeBackend:
    fake = FakeBackend(tokens=["Hello", " ", "world"], model_id=MODEL_ID, **kwargs)  # type: ignore[arg-type]
    asyncio.run(fake.load())
    return fake


def _ws_url(base: str, path: str) -> str:
    return base.replace("http://", "ws://") + path


# --- gate logic (unit) ----------------------------------------------------


def test_operator_gate_logic(tmp_settings: Settings) -> None:
    gateway = Gateway(
        tmp_settings,
        KeyStore(tmp_settings.db_path),
        UsageStore(tmp_settings.db_path),
    )
    # Migrate + create a valid key.
    from engine.store.db import connect
    from engine.store.migrations import apply_migrations

    conn = connect(tmp_settings.db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    _record, token = gateway.keys.create(label="op")

    assert gateway.operator_allowed(client_host="127.0.0.1", token=None) is True
    assert gateway.operator_allowed(client_host="203.0.113.7", token=None) is False
    assert gateway.operator_allowed(client_host="203.0.113.7", token="nope") is False
    assert gateway.operator_allowed(client_host="203.0.113.7", token=token) is True


# --- WebSocket happy paths (real server) ----------------------------------


def test_metrics_ws_sends_immediate_snapshot(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, backend=_loaded_fake())
    with running_app(app) as base:
        with ws_connect(_ws_url(base, "/ws/metrics")) as ws:
            snap = json.loads(ws.recv())
    assert set(snap) >= {"counters", "resources", "energy", "gpu", "backend", "uptime_s"}
    assert snap["energy"]["state"] in {"measured", "unavailable"}
    assert snap["gpu"]["available"] is False
    assert snap["backend"]["model_id"] == MODEL_ID


def test_metrics_ws_pushes_periodic_snapshots(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", metrics_interval_s=0.05)
    app = create_app(settings, backend=_loaded_fake())
    with running_app(app) as base:
        with ws_connect(_ws_url(base, "/ws/metrics")) as ws:
            first = json.loads(ws.recv())  # immediate
            second = json.loads(ws.recv())  # sampler tick
    assert "ts" in first and "ts" in second


def test_feed_ws_receives_request_lifecycle_events(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, backend=_loaded_fake())
    with running_app(app) as base:
        with ws_connect(_ws_url(base, "/ws/feed")) as ws:
            resp = httpx.post(base + "/v1/chat/completions", json=_CHAT, timeout=5.0)
            assert resp.status_code == 200
            types: list[str] = []
            for _ in range(6):
                event = json.loads(ws.recv())
                types.append(event["type"])
                if event["type"] == "request.end":
                    break
    assert "request.start" in types
    assert "request.end" in types


def test_feed_ws_allows_remote_with_valid_key(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, backend=_loaded_fake())
    # Create a key up front (server binds loopback, so the key is the operator path).
    with running_app(app) as base:
        _record, token = KeyStore(tmp_settings.db_path).create(label="op")
        with ws_connect(_ws_url(base, f"/ws/feed?api_key={token}")) as ws:
            httpx.post(base + "/v1/chat/completions", json=_CHAT, timeout=5.0)
            event = json.loads(ws.recv())
    assert event["type"].startswith("request.")


# --- rejection + REST (TestClient) ----------------------------------------


def test_metrics_ws_rejected_from_remote_without_key(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, backend=_loaded_fake())
    with TestClient(app, client=REMOTE) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/metrics") as ws:
                ws.receive_json()


def test_rest_metrics_gated_and_snapshot(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, backend=_loaded_fake())
    with TestClient(app, client=REMOTE) as c:
        assert c.get("/metrics").status_code == 401
        _record, token = KeyStore(tmp_settings.db_path).create(label="op")
        ok = c.get("/metrics", headers={"authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        assert "counters" in ok.json()


def test_diagnostics_is_redacted_and_secret_free(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, backend=_loaded_fake())
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        c.post("/v1/chat/completions", json=_CHAT)
        resp = c.get("/diagnostics")
    assert resp.status_code == 200
    bundle = resp.json()
    assert set(bundle) >= {"config", "hardware", "metrics", "recent_logs", "notes"}
    assert "hi" not in [str(v) for v in bundle["config"].values()]
    assert "no prompts" in bundle["notes"].lower()


def test_logs_endpoint_returns_events(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, backend=_loaded_fake())
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        c.post("/v1/chat/completions", json={"model": "nope", "messages": _CHAT["messages"]})
        resp = c.get("/logs?limit=50")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert any(e["category"] == "model" for e in events)
