"""DELETE refuses while the service owns work for the model.

A download is owned from its start until its worker has stopped, including
after a cancel request and during post-download finalization; a load or an
import of the same file is owned while it runs. A refused delete changes
nothing: no cancel signal, no file removed, the row kept. After the work stops,
the delete succeeds. The real service and downloader run through the API with a
controlled fake response; blocked work is released in ``finally`` and every
wait is bounded.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app
from engine.models import downloader
from engine.models.registry import ModelRegistry, ModelStatus
from tests.models.test_download_worker_lifetime import (
    WAIT,
    A,
    B,
    GatedResponse,
    _blocking_probe,
    _Hasher,
    _serve,
)
from tests.support.fake_backend import FakeBackend
from tests.support.gguf_writer import write_gguf

LOOPBACK = ("127.0.0.1", 40000)
URL = "https://cdn.example.com/m.gguf"
BUSY = "model_busy"


@pytest.fixture
def client(tmp_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_settings), client=LOOPBACK) as c:
        yield c


def _service(client: TestClient) -> Any:
    return client.app.state.model_service  # type: ignore[attr-defined]


def _models_dir(client: TestClient) -> Path:
    return Path(client.app.state.settings.models_dir)  # type: ignore[attr-defined]


def _start(client: TestClient) -> str:
    resp = client.post("/admin/models/download", json={"source_type": "url", "url": URL})
    assert resp.status_code == 200
    return str(resp.json()["id"])


def _wait_released(client: TestClient, model_id: str) -> None:
    deadline = time.monotonic() + WAIT
    while model_id in _service(client)._tasks:
        assert time.monotonic() < deadline, "the download worker did not finish"
        time.sleep(0.02)


def _files(client: TestClient) -> dict[str, bytes | None]:
    dest = _models_dir(client) / "m.gguf"
    part = dest.with_suffix(".gguf.part")
    return {p.name: (p.read_bytes() if p.exists() else None) for p in (dest, part)}


def _deletes(client: TestClient) -> int:
    audit = client.app.state.audit  # type: ignore[attr-defined]
    return sum(1 for e in audit.list(limit=1000) if e.action == "model.delete")


def _assert_busy(resp: Any) -> None:
    assert resp.status_code == 409
    body = resp.json()
    assert "deleted" not in body
    assert body["error"]["code"] == BUSY
    assert body["error"]["type"] == "invalid_request_error"


def _gate(phase: str, monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """Serves a download that blocks in ``phase``; returns (entered, release)."""
    if phase == "read":
        response = GatedResponse([A, B], block_at=1)
        _serve(monkeypatch, response)
        return response.blocked, response.release
    _serve(monkeypatch, GatedResponse([A, B]))
    if phase == "checksum":
        entered, release = threading.Event(), threading.Event()

        def hold(n: int) -> None:
            if n == 1:
                entered.set()
                release.wait(WAIT)

        hasher = _Hasher(on_update=hold)
        monkeypatch.setattr(downloader, "hashlib", SimpleNamespace(sha256=lambda: hasher))
        return entered, release
    return _blocking_probe(monkeypatch)  # finalize: blocked after the rename


@pytest.mark.parametrize("phase", ["read", "checksum", "finalize"])
def test_delete_is_refused_while_a_download_is_owned(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    entered, release = _gate(phase, monkeypatch)
    model_id = _start(client)
    try:
        assert entered.wait(WAIT), f"{phase}: the download never reached the gate"
        before = _files(client)
        _assert_busy(client.delete(f"/admin/models/{model_id}"))
        # Nothing changed: files, row, and no cancel signal as a side effect.
        assert _files(client) == before
        assert client.get(f"/admin/models/{model_id}").json()["status"] == "downloading"
        assert not _service(client)._cancels[model_id].is_set()
        assert _deletes(client) == 0
    finally:
        release.set()
    _wait_released(client, model_id)
    assert client.get(f"/admin/models/{model_id}").json()["status"] == "ready"

    assert client.delete(f"/admin/models/{model_id}").json() == {"deleted": True, "id": model_id}
    assert _files(client) == {"m.gguf": None, "m.gguf.part": None}
    assert client.get(f"/admin/models/{model_id}").status_code == 404
    assert _deletes(client) == 1
    # A repeated delete is the existing not-found response.
    assert client.delete(f"/admin/models/{model_id}").status_code == 404


def test_delete_is_refused_after_cancel_until_the_worker_stops(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = _gate("read", monkeypatch)
    model_id = _start(client)
    try:
        assert entered.wait(WAIT)
        assert client.post(f"/admin/models/{model_id}/cancel").status_code == 200
        # Accepted cancel is not a stopped worker: still owned.
        _assert_busy(client.delete(f"/admin/models/{model_id}"))
        assert _files(client)["m.gguf.part"] is not None
    finally:
        release.set()
    _wait_released(client, model_id)
    assert client.get(f"/admin/models/{model_id}").json()["status"] == "cancelled"
    assert client.delete(f"/admin/models/{model_id}").status_code == 200
    assert _files(client) == {"m.gguf": None, "m.gguf.part": None}


def test_orphaned_downloading_row_can_be_deleted(
    client: TestClient, tmp_settings: Settings
) -> None:
    models_dir = _models_dir(client)
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "orphan.gguf.part").write_bytes(A)
    record = ModelRegistry(tmp_settings.db_path).create(  # type: ignore[arg-type]
        name="orphan",
        filename="orphan.gguf",
        path=models_dir / "orphan.gguf",
        source_type="url",
        source_ref=URL,
        status=ModelStatus.DOWNLOADING,
    )
    assert client.delete(f"/admin/models/{record.id}").status_code == 200
    assert not (models_dir / "orphan.gguf.part").exists()


def _managed_import(client: TestClient, name: str = "managed.gguf") -> tuple[str, Path]:
    models_dir = _models_dir(client)
    models_dir.mkdir(parents=True, exist_ok=True)
    path = write_gguf(models_dir / name)
    resp = client.post("/admin/models/import", json={"path": str(path)})
    assert resp.status_code == 200
    return str(resp.json()["id"]), path


def test_file_removal_failure_keeps_the_row_and_a_retry_converges(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id, path = _managed_import(client)
    service = _service(client)

    def fails(record: object) -> None:
        raise PermissionError("cannot remove /srv/synthetic-private/managed.gguf")

    monkeypatch.setattr(service, "delete_files", fails)
    resp = client.delete(f"/admin/models/{model_id}")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "model_delete_failed"
    assert "synthetic" not in resp.text
    assert client.get(f"/admin/models/{model_id}").status_code == 200
    assert _deletes(client) == 0

    monkeypatch.undo()
    assert client.delete(f"/admin/models/{model_id}").status_code == 200
    assert not path.exists()
    assert _deletes(client) == 1


def test_row_removal_failure_after_files_are_gone_converges(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id, path = _managed_import(client)
    registry = _service(client).registry

    def fails(model_id: str) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(registry, "delete", fails)
    resp = client.delete(f"/admin/models/{model_id}")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "model_delete_failed"
    assert not path.exists()  # files went first
    assert client.get(f"/admin/models/{model_id}").status_code == 200

    monkeypatch.undo()
    assert client.delete(f"/admin/models/{model_id}").status_code == 200  # missing files skipped
    assert client.get(f"/admin/models/{model_id}").status_code == 404
    assert _deletes(client) == 1


class _SlowBackend(FakeBackend):
    """A backend whose load blocks until released."""

    def __init__(self, entered: threading.Event, release: threading.Event, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._entered = entered
        self._release = release

    async def load(self) -> None:
        self._entered.set()
        await asyncio.to_thread(self._release.wait, WAIT)
        await super().load()


def _in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, dict[str, Any]]:
    out: dict[str, Any] = {}

    def run() -> None:
        out["result"] = fn()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def test_delete_is_refused_while_the_model_is_loading(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_gguf(tmp_path / "external.gguf")
    imported = client.post("/admin/models/import", json={"path": str(path), "name": "slow"})
    model_id = imported.json()["id"]
    entered, release = threading.Event(), threading.Event()

    def build(record: Any) -> _SlowBackend:
        return _SlowBackend(entered, release, tokens=["hi"], model_id=record.name)

    monkeypatch.setattr(_service(client), "build_backend", build)
    thread, out = _in_thread(lambda: client.post(f"/admin/models/{model_id}/load"))
    try:
        assert entered.wait(WAIT), "the load never started"
        _assert_busy(client.delete(f"/admin/models/{model_id}"))
        # A second load of the same model is refused too.
        _assert_busy(client.post(f"/admin/models/{model_id}/load"))
    finally:
        release.set()
    thread.join(WAIT)
    assert out["result"].status_code == 200
    # Loaded now: the existing loaded-model refusal applies.
    loaded = client.delete(f"/admin/models/{model_id}")
    assert loaded.status_code == 409 and loaded.json()["error"]["code"] == "model_loaded"


def _blocking_hash(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    entered, release = threading.Event(), threading.Event()
    real = downloader.sha256_file

    def sha256_file(path: Path, *args: Any, **kwargs: Any) -> str:
        entered.set()
        release.wait(WAIT)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(downloader, "sha256_file", sha256_file)
    return entered, release


def test_delete_is_refused_while_its_file_is_being_imported(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_gguf(tmp_path / "shared.gguf")
    first = client.post("/admin/models/import", json={"path": str(path)}).json()["id"]
    entered, release = _blocking_hash(monkeypatch)
    thread, out = _in_thread(lambda: client.post("/admin/models/import", json={"path": str(path)}))
    try:
        assert entered.wait(WAIT)
        _assert_busy(client.delete(f"/admin/models/{first}"))
        # The same path cannot be imported twice at once either.
        _assert_busy(client.post("/admin/models/import", json={"path": str(path)}))
    finally:
        release.set()
    thread.join(WAIT)
    assert out["result"].status_code == 200
    assert client.delete(f"/admin/models/{first}").status_code == 200
    assert path.exists()  # an external import stays on disk


def test_download_is_refused_for_a_file_being_imported(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    models_dir = _models_dir(client)
    models_dir.mkdir(parents=True, exist_ok=True)
    path = write_gguf(models_dir / "m.gguf")
    entered, release = _blocking_hash(monkeypatch)
    thread, out = _in_thread(lambda: client.post("/admin/models/import", json={"path": str(path)}))
    try:
        assert entered.wait(WAIT)
        resp = client.post("/admin/models/download", json={"source_type": "url", "url": URL})
        assert resp.status_code == 409 and resp.json()["error"]["code"] == BUSY
    finally:
        release.set()
    thread.join(WAIT)
    assert out["result"].status_code == 200


def test_import_is_refused_for_a_download_that_is_finalizing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = _gate("finalize", monkeypatch)
    model_id = _start(client)
    try:
        assert entered.wait(WAIT)
        dest = _models_dir(client) / "m.gguf"
        assert dest.exists()  # promoted, still owned
        resp = client.post("/admin/models/import", json={"path": str(dest)})
        assert resp.status_code == 409 and resp.json()["error"]["code"] == BUSY
    finally:
        release.set()
    _wait_released(client, model_id)
    assert client.get(f"/admin/models/{model_id}").json()["status"] == "ready"
