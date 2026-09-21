"""Model-lifecycle API: import, download (progress→ready), load/unload, delete."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app
from tests.models.conftest import FileServer
from tests.support.fake_backend import FakeBackend
from tests.support.gguf_writer import write_gguf

LOOPBACK = ("127.0.0.1", 40000)
REMOTE = ("203.0.113.7", 5000)


@pytest.fixture
def client(tmp_settings: Settings) -> Iterator[TestClient]:
    app = create_app(tmp_settings)  # no configured model
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def _poll_status(
    client: TestClient, model_id: str, target: set[str], timeout: float = 10.0
) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = client.get(f"/admin/models/{model_id}").json()
        if last["status"] in target:
            return last
        time.sleep(0.05)
    return last


def test_list_empty_and_gating(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings)
    with TestClient(app, client=LOOPBACK) as c:
        assert c.get("/admin/models").json() == {"models": []}
    with TestClient(app, client=REMOTE) as c:
        assert c.get("/admin/models").status_code == 401


def test_import_local_model(client: TestClient, tmp_path) -> None:
    path = write_gguf(tmp_path / "local.gguf", name="Imported", context_length=4096, file_type=15)
    resp = client.post("/admin/models/import", json={"path": str(path), "name": "my-model"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "my-model"
    assert body["status"] == "ready"
    assert body["sha256"] is not None
    assert body["arch"] == "llama"
    assert body["quant"] == "Q4_K_M"
    assert body["context_length"] == 4096
    # compat is reported honestly (this sandbox has no llama/AVX).
    assert body["compat"]["status"] in {"ok", "needs_backend", "too_large", "unknown"}
    assert len(client.get("/admin/models").json()["models"]) == 1


def test_import_bad_path(client: TestClient) -> None:
    resp = client.post("/admin/models/import", json={"path": "/no/such/file.gguf"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "model_import_failed"


def test_download_verifies_and_becomes_ready(client: TestClient, gguf_server: FileServer) -> None:
    resp = client.post(
        "/admin/models/download",
        json={
            "source_type": "url",
            "url": f"{gguf_server.base_url}/tiny.gguf",
            "name": "downloaded",
            "expected_sha256": gguf_server.digest,
        },
    )
    assert resp.status_code == 200
    model_id = resp.json()["id"]
    assert resp.json()["status"] == "downloading"

    final = _poll_status(client, model_id, {"ready", "error"})
    assert final["status"] == "ready", final
    assert final["sha256"] == gguf_server.digest
    assert final["arch"] == "llama"
    assert final["progress"] == 1.0


def test_download_bad_checksum_ends_in_error(client: TestClient, gguf_server: FileServer) -> None:
    resp = client.post(
        "/admin/models/download",
        json={
            "source_type": "url",
            "url": f"{gguf_server.base_url}/tiny.gguf",
            "name": "bad",
            "expected_sha256": "0" * 64,
        },
    )
    model_id = resp.json()["id"]
    final = _poll_status(client, model_id, {"ready", "error"})
    assert final["status"] == "error"
    assert "checksum" in (final["error"] or "").lower()


def test_download_requires_fields(client: TestClient) -> None:
    assert client.post("/admin/models/download", json={"source_type": "url"}).status_code == 400
    assert (
        client.post("/admin/models/download", json={"source_type": "huggingface"}).status_code
        == 400
    )


def test_load_without_backend_reports_503(client: TestClient, tmp_path) -> None:
    # Real load path: llama-cpp-python is absent here, so load fails honestly.
    path = write_gguf(tmp_path / "x.gguf")
    model_id = client.post("/admin/models/import", json={"path": str(path)}).json()["id"]
    resp = client.post(f"/admin/models/{model_id}/load")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "model_load_failed"


def test_load_unload_swaps_active_backend(client: TestClient, tmp_path, monkeypatch) -> None:
    path = write_gguf(tmp_path / "y.gguf", name="Loadable")
    model_id = client.post(
        "/admin/models/import", json={"path": str(path), "name": "loadable"}
    ).json()["id"]

    # Substitute a fake backend (unloaded) so the load endpoint's own await load()
    # exercises the swap without needing llama.cpp.
    def _fake_build(record):  # noqa: ANN001
        return FakeBackend(tokens=["hi"], model_id=record.name)

    monkeypatch.setattr(client.app.state.model_service, "build_backend", _fake_build)

    loaded = client.post(f"/admin/models/{model_id}/load")
    assert loaded.status_code == 200
    assert loaded.json()["model"]["loaded"] is True
    assert loaded.json()["model"]["active"] is True

    # The active model now serves /v1 under its name.
    chat = client.post(
        "/v1/chat/completions",
        json={"model": "loadable", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert chat.status_code == 200

    # Cannot delete a loaded model; unload first, then delete.
    assert client.delete(f"/admin/models/{model_id}").status_code == 409
    assert client.post(f"/admin/models/{model_id}/unload").json()["result"] == "unloaded"
    assert client.delete(f"/admin/models/{model_id}").status_code == 200
    assert client.get("/admin/models").json()["models"] == []


def test_cancel_non_downloading_is_409(client: TestClient, tmp_path) -> None:
    path = write_gguf(tmp_path / "z.gguf")
    model_id = client.post("/admin/models/import", json={"path": str(path)}).json()["id"]
    assert client.post(f"/admin/models/{model_id}/cancel").status_code == 409
