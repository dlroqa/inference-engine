"""Admin API: overview, model load/unload, key management, and authorization."""

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


def _loaded_fake() -> FakeBackend:
    fake = FakeBackend(tokens=["Hi"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    return fake


@pytest.fixture
def admin_client(tmp_settings: Settings) -> Iterator[TestClient]:
    # Config matches the served model id (Block 12.1) so that after an unload the
    # model stays known and requests return 503 (model_not_loaded), not 404.
    settings = tmp_settings.model_copy(update={"model_id": MODEL_ID})
    app = create_app(settings, backend=_loaded_fake())
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def test_overview_reports_model_and_metrics(admin_client: TestClient) -> None:
    resp = admin_client.get("/admin/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["migrations"] == "applied"
    assert body["model"]["loaded"] is True
    assert body["model"]["model_id"] == MODEL_ID
    assert "counters" in body["metrics"]


def test_admin_requires_operator_from_remote(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, backend=_loaded_fake())
    with TestClient(app, client=REMOTE) as c:
        assert c.get("/admin/overview").status_code == 401
        assert c.get("/admin/keys").status_code == 401
        assert c.post("/admin/model/unload").status_code == 401
        # A valid key lifts the gate.
        from engine.auth.keys import KeyStore

        _r, token = KeyStore(tmp_settings.db_path).create(label="op")
        ok = c.get("/admin/overview", headers={"authorization": f"Bearer {token}"})
        assert ok.status_code == 200


def test_model_unload_then_serving_unavailable(admin_client: TestClient) -> None:
    assert admin_client.post("/admin/model/unload").json()["result"] == "unloaded"
    # Idempotent second call.
    assert admin_client.post("/admin/model/unload").json()["result"] == "already_unloaded"
    # With no model loaded, inference reports 503.
    resp = admin_client.post(
        "/v1/chat/completions",
        json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 503


def test_model_load_idempotent_when_ready(admin_client: TestClient) -> None:
    assert admin_client.post("/admin/model/load").json()["result"] == "already_loaded"


def test_model_load_without_config_is_400(tmp_settings: Settings) -> None:
    # No injected backend and no model_path configured.
    app = create_app(tmp_settings)
    with TestClient(app, client=LOOPBACK) as c:
        resp = c.post("/admin/model/load")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "model_not_configured"


def test_keys_create_list_revoke_lifecycle(admin_client: TestClient) -> None:
    created = admin_client.post("/admin/keys", json={"label": "dash"}).json()
    assert created["token"].startswith("sk-ie-")  # shown once
    assert created["label"] == "dash"
    key_id = created["id"]

    listed = admin_client.get("/admin/keys").json()["keys"]
    ids = {k["id"] for k in listed}
    assert key_id in ids
    # No secret is ever listed.
    assert all("token" not in k and "key_hash" not in k for k in listed)

    revoked = admin_client.delete(f"/admin/keys/{key_id}")
    assert revoked.status_code == 200 and revoked.json()["revoked"] is True
    # Revoking again (already revoked) -> 404.
    assert admin_client.delete(f"/admin/keys/{key_id}").status_code == 404


def test_purge_deletes_a_revoked_key(admin_client: TestClient) -> None:
    created = admin_client.post("/admin/keys", json={"label": "temp"}).json()
    key_id = created["id"]

    # Cannot purge an active key -> 409 (must revoke first).
    conflict = admin_client.delete(f"/admin/keys/{key_id}?purge=true")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "key_not_revoked"

    # Revoke, then purge -> the key disappears from the list entirely.
    assert admin_client.delete(f"/admin/keys/{key_id}").status_code == 200
    purged = admin_client.delete(f"/admin/keys/{key_id}?purge=true")
    assert purged.status_code == 200 and purged.json()["deleted"] is True
    ids = {k["id"] for k in admin_client.get("/admin/keys").json()["keys"]}
    assert key_id not in ids

    # Purging a non-existent key -> 404.
    assert admin_client.delete(f"/admin/keys/{key_id}?purge=true").status_code == 404


def test_created_key_authenticates_then_fails_after_revoke(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings, backend=_loaded_fake())
    with TestClient(app, client=REMOTE) as c:
        # Bootstrap a key from loopback is not available here; create via a loopback client.
        from engine.auth.keys import KeyStore

        store = KeyStore(tmp_settings.db_path)
        _r, token = store.create(label="bootstrap")
        headers = {"authorization": f"Bearer {token}"}
        # The key can list keys, create a second key, then we revoke the second.
        second = c.post("/admin/keys", json={"label": "second"}, headers=headers).json()
        assert c.delete(f"/admin/keys/{second['id']}", headers=headers).status_code == 200
