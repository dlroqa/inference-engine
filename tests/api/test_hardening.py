"""Security hardening baseline (Block 9b): IP allowlist, kill switches, audit API."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app
from tests.api.conftest import MODEL_ID
from tests.support.fake_backend import FakeBackend

LOOPBACK = ("127.0.0.1", 40000)


def _app(tmp_settings: Settings, **overrides: object):
    fake = FakeBackend(tokens=["x"], model_id=MODEL_ID, supports_structured_output=True)
    asyncio.run(fake.load())
    data = tmp_settings.data_dir
    return create_app(Settings(data_dir=data, **overrides), backend=fake)


# --- IP allowlist -----------------------------------------------------------


def test_ip_allowlist_denies_outside_client(tmp_settings: Settings) -> None:
    app = _app(tmp_settings, ip_allowlist=["10.0.0.0/8"])
    with TestClient(app, client=("8.8.8.8", 1234)) as c:
        resp = c.get("/healthz")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ip_not_allowed"


def test_ip_allowlist_permits_inside_client(tmp_settings: Settings) -> None:
    app = _app(tmp_settings, ip_allowlist=["10.0.0.0/8"])
    with TestClient(app, client=("10.1.2.3", 1234)) as c:
        assert c.get("/healthz").status_code == 200


def test_ip_allowlist_forwarded_for_only_when_trusted(tmp_settings: Settings) -> None:
    # Without trust_forwarded_for, a spoofed XFF cannot bypass the peer check.
    app = _app(tmp_settings, ip_allowlist=["10.0.0.0/8"])
    with TestClient(app, client=("8.8.8.8", 1234)) as c:
        assert c.get("/healthz", headers={"x-forwarded-for": "10.1.2.3"}).status_code == 403
    # With trust_forwarded_for, the forwarded client IP is honored.
    app2 = _app(tmp_settings, ip_allowlist=["10.0.0.0/8"], trust_forwarded_for=True)
    with TestClient(app2, client=("8.8.8.8", 1234)) as c:
        assert c.get("/healthz", headers={"x-forwarded-for": "10.1.2.3"}).status_code == 200
        assert c.get("/healthz", headers={"x-forwarded-for": "8.8.8.8"}).status_code == 403


def test_no_allowlist_allows_all(tmp_settings: Settings) -> None:
    app = _app(tmp_settings)
    with TestClient(app, client=("8.8.8.8", 1234)) as c:
        assert c.get("/healthz").status_code == 200


# --- Kill switches ----------------------------------------------------------


def test_structured_output_kill_switch(tmp_settings: Settings) -> None:
    app = _app(tmp_settings, allow_structured_output=False)
    with TestClient(app) as c:
        resp = c.post(
            "/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_object"},
            },
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "structured_output_disabled"


def test_model_management_kill_switch(tmp_settings: Settings) -> None:
    app = _app(tmp_settings, allow_model_management=False)
    with TestClient(app, client=LOOPBACK) as c:
        resp = c.post("/admin/models/import", json={"path": "/x.gguf", "name": "n"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "model_management_disabled"


def test_network_downloads_kill_switch(tmp_settings: Settings) -> None:
    app = _app(tmp_settings, allow_network_downloads=False)
    with TestClient(app, client=LOOPBACK) as c:
        resp = c.post(
            "/admin/models/download",
            json={"source_type": "url", "name": "n", "url": "http://x/y.gguf"},
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "downloads_disabled"


def test_download_also_requires_model_management(tmp_settings: Settings) -> None:
    # Downloads need both switches: allow_model_management and allow_network_downloads.
    app = _app(tmp_settings, allow_model_management=False, allow_network_downloads=True)
    with TestClient(app, client=LOOPBACK) as c:
        resp = c.post(
            "/admin/models/download",
            json={"source_type": "url", "name": "n", "url": "http://x/y.gguf"},
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "model_management_disabled"


def test_diagnostics_kill_switch(tmp_settings: Settings) -> None:
    app = _app(tmp_settings, diagnostics_enabled=False)
    with TestClient(app, client=LOOPBACK) as c:
        resp = c.get("/diagnostics")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "diagnostics_disabled"


# --- Audit API --------------------------------------------------------------


@pytest.fixture
def admin_client(tmp_settings: Settings) -> Iterator[TestClient]:
    with TestClient(_app(tmp_settings), client=LOOPBACK) as c:
        yield c


def test_audit_records_key_and_model_actions(admin_client: TestClient) -> None:
    created = admin_client.post("/admin/keys", json={"label": "x"}).json()
    admin_client.delete(f"/admin/keys/{created['id']}")  # revoke
    body = admin_client.get("/admin/audit?verify=true").json()
    actions = [e["action"] for e in body["events"]]
    assert "key.create" in actions and "key.revoke" in actions
    assert body["verify"]["ok"] is True
    # No secrets are recorded.
    assert "token" not in str(body)


def test_audit_endpoint_detects_tampering(admin_client: TestClient) -> None:
    admin_client.post("/admin/keys", json={"label": "x"})
    db_path = admin_client.app.state.settings.db_path  # type: ignore[attr-defined]
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE audit_events SET actor = 'mallory' WHERE id = 1;")
        conn.commit()
    finally:
        conn.close()
    body = admin_client.get("/admin/audit?verify=true").json()
    assert body["verify"]["ok"] is False
    assert body["verify"]["first_bad_id"] == 1
