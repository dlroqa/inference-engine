"""Operator gate precedence (dashboard wiring A1).

Credentials are evaluated before the loopback exception:

| credentials                          | operator surface |
| ------------------------------------ | ---------------- |
| valid operator key                   | allowed          |
| valid billing-client key (any host)  | 403 operator_role_required |
| invalid / revoked key (any host)     | 401 operator_access_required |
| no key, effective auth on            | 401 (even loopback) |
| no key, effective auth off           | loopback only    |

The same decision gates the operator WebSockets (rejections close with 1008),
while client keys keep working for inference.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from engine.auth.keys import KeyStore
from engine.billing.store import BillingStore
from engine.config import Settings
from engine.main import create_app
from engine.store.db import connect
from engine.store.migrations import apply_migrations
from tests.support.fake_backend import FakeBackend

MODEL_ID = "fake-model"
LOOPBACK = ("127.0.0.1", 40000)
REMOTE = ("203.0.113.7", 5000)
OPERATOR_GETS = ("/admin/overview", "/admin/identity", "/admin/system", "/metrics", "/logs")


@dataclass
class Keys:
    operator: str
    client: str
    revoked: str


def _app(tmp_path: Path, *, require_auth: bool | None) -> tuple[object, Settings]:
    settings = Settings(data_dir=tmp_path / "data", require_auth=require_auth)
    fake = FakeBackend(tokens=["Hi"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    return create_app(settings, backend=fake), settings


def _migrate(settings: Settings) -> None:
    conn = connect(settings.db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()


def _keys(settings: Settings) -> Keys:
    store = KeyStore(settings.db_path)
    _op, operator = store.create(label="owner")
    client_rec, client = store.create(label="acme")
    billing = BillingStore(settings.db_path)
    billing.create_client(id="acme")
    assert billing.attach_key(client_rec.id, "acme")
    revoked_rec, revoked = store.create(label="old")
    store.revoke(revoked_rec.id)
    return Keys(operator=operator, client=client, revoked=revoked)


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture(params=[True, False], ids=["auth-on", "auth-off"])
def env(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[tuple[object, Keys, bool]]:
    app, settings = _app(tmp_path, require_auth=request.param)
    _migrate(settings)  # keys are created before any client runs the lifespan
    yield app, _keys(settings), request.param


@pytest.mark.parametrize("peer", [LOOPBACK, REMOTE], ids=["loopback", "remote"])
def test_operator_key_allowed_everywhere(env: tuple[object, Keys, bool], peer: tuple) -> None:
    app, keys, _auth = env
    with TestClient(app, client=peer) as c:  # type: ignore[arg-type]
        for path in OPERATOR_GETS:
            assert c.get(path, headers=_bearer(keys.operator)).status_code == 200, path


@pytest.mark.parametrize("peer", [LOOPBACK, REMOTE], ids=["loopback", "remote"])
def test_client_key_is_forbidden_even_on_loopback(
    env: tuple[object, Keys, bool], peer: tuple
) -> None:
    app, keys, _auth = env
    with TestClient(app, client=peer) as c:  # type: ignore[arg-type]
        for path in (*OPERATOR_GETS, "/diagnostics", "/admin/keys"):
            resp = c.get(path, headers=_bearer(keys.client))
            assert resp.status_code == 403, path
            assert resp.json()["error"]["code"] == "operator_role_required"
        # Writes are refused too, and nothing is created.
        resp = c.post("/admin/keys", json={"label": "x"}, headers=_bearer(keys.client))
        assert resp.status_code == 403


@pytest.mark.parametrize("peer", [LOOPBACK, REMOTE], ids=["loopback", "remote"])
@pytest.mark.parametrize("which", ["invalid", "revoked"])
def test_bad_keys_are_unauthenticated_even_on_loopback(
    env: tuple[object, Keys, bool], peer: tuple, which: str
) -> None:
    app, keys, _auth = env
    token = "sk-ie-not-a-real-key" if which == "invalid" else keys.revoked
    with TestClient(app, client=peer) as c:  # type: ignore[arg-type]
        for path in OPERATOR_GETS:
            resp = c.get(path, headers=_bearer(token))
            assert resp.status_code == 401, path
            assert resp.json()["error"]["code"] == "operator_access_required"


def test_keyless_access_depends_on_effective_auth(env: tuple[object, Keys, bool]) -> None:
    app, _keys_, auth_on = env
    with TestClient(app, client=LOOPBACK) as local:  # type: ignore[arg-type]
        for path in OPERATOR_GETS:
            assert local.get(path).status_code == (401 if auth_on else 200), path
    with TestClient(app, client=REMOTE) as remote:  # type: ignore[arg-type]
        for path in OPERATOR_GETS:
            assert remote.get(path).status_code == 401, path


def test_client_key_still_serves_inference(env: tuple[object, Keys, bool]) -> None:
    app, keys, _auth = env
    body = {"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]}
    with TestClient(app, client=REMOTE) as c:  # type: ignore[arg-type]
        resp = c.post("/v1/chat/completions", json=body, headers=_bearer(keys.client))
    assert resp.status_code == 200


@pytest.mark.parametrize("path", ["/ws/metrics", "/ws/feed"])
def test_websockets_follow_the_same_policy(env: tuple[object, Keys, bool], path: str) -> None:
    app, keys, auth_on = env
    with TestClient(app, client=LOOPBACK) as c:  # type: ignore[arg-type]
        for token in (keys.client, keys.revoked, "sk-ie-nope"):
            with pytest.raises(WebSocketDisconnect) as exc:
                with c.websocket_connect(f"{path}?api_key={token}") as ws:
                    ws.receive_json()
            assert exc.value.code == 1008
        if auth_on:
            with pytest.raises(WebSocketDisconnect) as exc:
                with c.websocket_connect(path) as ws:
                    ws.receive_json()
            assert exc.value.code == 1008


def test_identity_reports_key_metadata_without_token(env: tuple[object, Keys, bool]) -> None:
    app, keys, auth_on = env
    with TestClient(app, client=REMOTE) as c:  # type: ignore[arg-type]
        body = c.get("/admin/identity", headers=_bearer(keys.operator)).json()
    assert body["kind"] == "key"
    assert body["auth_required"] is auth_on
    assert body["key"]["label"] == "owner"
    assert body["key"]["role"] == "operator"
    assert body["key"]["prefix"] == keys.operator[:12]
    assert keys.operator not in str(body)


def test_identity_reports_local_dev_when_keyless(tmp_path: Path) -> None:
    app, _settings = _app(tmp_path, require_auth=False)
    with TestClient(app, client=LOOPBACK) as c:  # type: ignore[arg-type]
        body = c.get("/admin/identity").json()
    assert body == {"kind": "local", "auth_required": False, "key": None}


def test_key_list_exposes_role(env: tuple[object, Keys, bool]) -> None:
    app, keys, _auth = env
    with TestClient(app, client=REMOTE) as c:  # type: ignore[arg-type]
        listed = c.get("/admin/keys", headers=_bearer(keys.operator)).json()["keys"]
    roles = {k["label"]: k["role"] for k in listed}
    assert roles["owner"] == "operator"
    assert roles["acme"] == "client"
    assert {k["label"]: k["client_id"] for k in listed}["acme"] == "acme"
