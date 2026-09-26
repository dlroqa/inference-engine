"""The cancel endpoint reports the service's decision truthfully.

``ModelService.cancel`` decides atomically whether a cancel request is
accepted: it refuses once the downloaded file has been committed (renamed into
place) and while no worker is tracked. The endpoint must not claim a refused
request was accepted, even while the registry still says ``downloading``.

These run the real service and downloader through the API, with a controlled
fake response (``GatedResponse``) instead of a network. Blocked work is always
released in ``finally`` and every wait is bounded.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.auth.keys import KeyStore
from engine.billing.store import BillingStore
from engine.config import Settings
from engine.main import create_app
from engine.models.registry import ModelRegistry, ModelStatus
from engine.store.db import connect
from engine.store.migrations import apply_migrations
from tests.models.test_download_worker_lifetime import (
    WAIT,
    A,
    B,
    GatedResponse,
    _blocking_probe,
    _serve,
)

LOOPBACK = ("127.0.0.1", 40000)
URL = "https://cdn.example.com/m.gguf"
REFUSED = "model_cancel_not_accepted"


@pytest.fixture
def client(tmp_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_settings), client=LOOPBACK) as c:
        yield c


def _start(client: TestClient) -> str:
    resp = client.post("/admin/models/download", json={"source_type": "url", "url": URL})
    assert resp.status_code == 200
    return str(resp.json()["id"])


def _status(client: TestClient, model_id: str) -> str:
    return str(client.get(f"/admin/models/{model_id}").json()["status"])


def _wait_released(client: TestClient, model_id: str) -> None:
    """Waits (bounded) until the service no longer tracks the download."""
    service = client.app.state.model_service  # type: ignore[attr-defined]
    deadline = time.monotonic() + WAIT
    while model_id in service._tasks:
        assert time.monotonic() < deadline, "the download worker did not finish"
        time.sleep(0.02)


def _dest(client: TestClient) -> Path:
    return Path(client.app.state.settings.models_dir) / "m.gguf"  # type: ignore[attr-defined]


def _assert_refused(resp_json: dict[str, object], status_code: int) -> None:
    assert status_code == 409
    assert "cancelling" not in resp_json
    error = resp_json["error"]
    assert isinstance(error, dict)
    assert error["code"] == REFUSED
    assert error["type"] == "invalid_request_error"
    assert "not accepted" in str(error["message"])


def test_cancel_while_committed_and_finalizing_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, response := GatedResponse([A, B]))
    entered, release = _blocking_probe(monkeypatch)
    model_id = _start(client)
    try:
        assert entered.wait(WAIT), "finalization never started"
        # The file is committed, but the registry still says downloading.
        assert _dest(client).read_bytes() == A + B
        assert _status(client, model_id) == ModelStatus.DOWNLOADING.value
        resp = client.post(f"/admin/models/{model_id}/cancel")
        _assert_refused(resp.json(), resp.status_code)
    finally:
        release.set()
    _wait_released(client, model_id)
    # The download's real outcome stands; nothing marked it cancelled.
    assert _status(client, model_id) == ModelStatus.READY.value
    assert response.closed.is_set()


@pytest.mark.parametrize("management", [True, False], ids=["management-on", "management-off"])
def test_cancel_before_commit_is_accepted_and_the_worker_stops(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, management: bool
) -> None:
    response = GatedResponse([A, B], block_at=1)
    _serve(monkeypatch, response)
    model_id = _start(client)
    try:
        assert response.blocked.wait(WAIT), "the read never started"
        # Cancellation is not behind the model-management switch.
        client.app.state.settings.allow_model_management = management  # type: ignore[attr-defined]
        resp = client.post(f"/admin/models/{model_id}/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"cancelling": True, "id": model_id}
        # Accepted is a request, not a stop: the worker is still blocked.
        assert _status(client, model_id) == ModelStatus.DOWNLOADING.value
        again = client.post(f"/admin/models/{model_id}/cancel")
        assert again.status_code == 200 and again.json()["cancelling"] is True
    finally:
        response.release.set()
    _wait_released(client, model_id)
    assert _status(client, model_id) == ModelStatus.CANCELLED.value
    part = _dest(client).with_suffix(".gguf.part")
    assert part.read_bytes() == A and not _dest(client).exists()
    assert response.closed.is_set()
    # Terminal now: the existing non-cancellable-state response.
    done = client.post(f"/admin/models/{model_id}/cancel")
    assert done.status_code == 409 and done.json()["error"]["code"] == "not_downloading"


def _seed_untracked(settings: Settings, tmp_path: Path) -> str:
    """A ``downloading`` record with no worker (e.g. after a forced kill)."""
    record = ModelRegistry(settings.db_path).create(  # type: ignore[arg-type]
        name="orphan",
        filename="orphan.gguf",
        path=tmp_path / "orphan.gguf",
        source_type="url",
        source_ref=URL,
        status=ModelStatus.DOWNLOADING,
    )
    return record.id


def test_downloading_record_without_a_worker_is_refused(
    client: TestClient, tmp_settings: Settings, tmp_path: Path
) -> None:
    model_id = _seed_untracked(tmp_settings, tmp_path)
    resp = client.post(f"/admin/models/{model_id}/cancel")
    _assert_refused(resp.json(), resp.status_code)
    assert _status(client, model_id) == ModelStatus.DOWNLOADING.value


def test_missing_model_is_not_found(client: TestClient) -> None:
    resp = client.post("/admin/models/does-not-exist/cancel")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


def test_cancel_keeps_the_operator_gate(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", require_auth=True)
    conn = connect(settings.db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    store = KeyStore(settings.db_path)
    _op, operator = store.create(label="owner")
    client_rec, client_key = store.create(label="acme")
    billing = BillingStore(settings.db_path)
    billing.create_client(id="acme")
    assert billing.attach_key(client_rec.id, "acme")
    model_id = _seed_untracked(settings, tmp_path)
    path = f"/admin/models/{model_id}/cancel"

    with TestClient(create_app(settings), client=LOOPBACK) as c:
        assert c.post(path).status_code == 401
        denied = c.post(path, headers={"authorization": f"Bearer {client_key}"})
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "operator_role_required"
        refused = c.post(path, headers={"authorization": f"Bearer {operator}"})
        _assert_refused(refused.json(), refused.status_code)
