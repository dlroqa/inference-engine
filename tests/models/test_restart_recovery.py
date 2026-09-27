"""Restart recovery for downloads a previous engine process left running.

A child process (``tests.support.download_child``) holds the store lock and
runs a real download to a deterministic barrier, then is killed. SIGKILL (or
TerminateProcess on Windows) qualifies process termination, not power loss:
nothing here claims storage-device durability. The engine restarted on the
same store must mark the row ``error`` with a fixed message, keep the files it
finds, and do the same thing again on every later startup. Every wait is
bounded and the child is always killed in ``finally``.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app
from engine.models.registry import ModelRegistry, ModelStatus
from engine.models.service import INTERRUPTED
from engine.store.db import connect
from engine.store.migrations import apply_migrations
from engine.store.ownership import StoreLockedError

REPO = Path(__file__).resolve().parents[2]
LOOPBACK = ("127.0.0.1", 40000)  # keyless operator access (auth off, loopback)
WAIT = 30  # seconds, for the child to start and reach its barrier
A = b"A" * 8
B = b"B" * 8


@contextmanager
def _child(data_dir: Path, barrier: str) -> Iterator[subprocess.Popen[bytes]]:
    """Runs the download child until it reaches ``barrier``; always kills it."""
    marker = data_dir.parent / f"{barrier}.reached"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.support.download_child", str(data_dir), barrier, str(marker)],
        cwd=REPO,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + WAIT
        while not marker.exists():
            if proc.poll() is not None:
                err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                pytest.fail(f"the child exited early ({proc.returncode}): {err[-2000:]}")
            assert time.monotonic() < deadline, "the child never reached its barrier"
            time.sleep(0.05)
        yield proc
    finally:
        proc.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
        proc.wait(timeout=WAIT)
        if proc.stderr:
            proc.stderr.close()


def _models_dir(settings: Settings) -> Path:
    assert settings.models_dir is not None
    return Path(settings.models_dir)


def _only_model(client: TestClient) -> dict[str, Any]:
    [model] = client.get("/admin/models").json()["models"]
    return dict(model)


def _row(settings: Settings, model_id: str) -> tuple[Any, ...]:
    conn = sqlite3.connect(str(settings.db_path))
    try:
        return tuple(conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone())
    finally:
        conn.close()


@pytest.mark.parametrize("barrier", ["read", "finalize"], ids=["before-rename", "after-rename"])
def test_a_killed_download_is_marked_interrupted_and_its_files_are_kept(
    tmp_path: Path, barrier: str
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    with _child(settings.data_dir, barrier):
        # The child owns the store: a second engine refuses to start.
        with pytest.raises(StoreLockedError), TestClient(create_app(settings), client=LOOPBACK):
            pass
    # The child is dead; its lock went with it.
    found = {p.name: p.read_bytes() for p in _models_dir(settings).glob("m.gguf*")}
    with TestClient(create_app(settings), client=LOOPBACK) as client:
        model = _only_model(client)
        assert model["status"] == ModelStatus.ERROR.value
        assert model["error"] == INTERRUPTED
        dest = _models_dir(settings) / "m.gguf"
        part = dest.with_suffix(".gguf.part")
        if barrier == "read":
            # Killed before the rename: only the partial file. What reached the
            # disk before the kill is a prefix of the data (buffered writes may
            # be lost), and recovery leaves it exactly as found.
            assert not dest.exists() and part.exists()
            assert (A + B).startswith(part.read_bytes())
        else:
            # Killed after the rename, before the registry update.
            assert dest.read_bytes() == A + B and not part.exists()
        # Recovery changed no file.
        assert {p.name: p.read_bytes() for p in _models_dir(settings).glob("m.gguf*")} == found
        # Never marked ready or loaded from the file alone.
        assert model["loaded"] is False and model["sha256"] is None
        row = _row(settings, model["id"])

    # Idempotent: a second restart changes nothing, including updated_at.
    with TestClient(create_app(settings), client=LOOPBACK) as client:
        assert _only_model(client)["error"] == INTERRUPTED
        assert _row(settings, model["id"]) == row
        # The documented recovery: delete removes the kept files.
        assert client.delete(f"/admin/models/{model['id']}").status_code == 200
    assert not any(_models_dir(settings).glob("m.gguf*"))


def _seed(settings: Settings, status: ModelStatus, path: Path, error: str | None = None) -> str:
    conn = connect(settings.db_path)  # type: ignore[arg-type]
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    registry = ModelRegistry(settings.db_path)  # type: ignore[arg-type]
    record = registry.create(
        name=path.stem,
        filename=path.name,
        path=path,
        source_type="url",
        source_ref="address not stored",
        status=status,
    )
    if error is not None:
        registry.update(record.id, error=error)
    return record.id


@pytest.mark.parametrize("present", ["neither", "both", "verifying"])
def test_interrupted_rows_become_errors_whatever_files_remain(tmp_path: Path, present: str) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    models = _models_dir(settings)
    models.mkdir(parents=True)
    path = models / "x.gguf"
    if present == "both":
        path.write_bytes(A)
        path.with_suffix(".gguf.part").write_bytes(B)
    status = ModelStatus.VERIFYING if present == "verifying" else ModelStatus.DOWNLOADING
    model_id = _seed(settings, status, path)
    with TestClient(create_app(settings), client=LOOPBACK) as client:
        body = client.get(f"/admin/models/{model_id}").json()
    assert body["status"] == "error" and body["error"] == INTERRUPTED
    if present == "both":  # evidence preserved, nothing deleted
        assert path.read_bytes() == A and path.with_suffix(".gguf.part").read_bytes() == B


def test_completed_rows_are_never_changed(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    models = _models_dir(settings)
    models.mkdir(parents=True)
    ids = {
        status: _seed(settings, status, models / f"{status}.gguf", error="kept")
        for status in (ModelStatus.READY, ModelStatus.ERROR, ModelStatus.CANCELLED)
    }
    rows = {s: _row(settings, i) for s, i in ids.items()}
    for _ in range(2):
        with TestClient(create_app(settings), client=LOOPBACK):
            pass
    assert {s: _row(settings, i) for s, i in ids.items()} == rows


def test_links_are_not_followed_or_touched(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    models = _models_dir(settings)
    models.mkdir(parents=True)
    outside = tmp_path / "outside.gguf"
    outside.write_bytes(A)
    link = models / "linked.gguf"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:  # e.g. Windows without symlink rights
        pytest.skip(f"symlinks are not available here: {type(exc).__name__}")
    model_id = _seed(settings, ModelStatus.DOWNLOADING, link)
    with TestClient(create_app(settings), client=LOOPBACK) as client:
        assert client.get(f"/admin/models/{model_id}").json()["status"] == "error"
    assert link.is_symlink() and outside.read_bytes() == A


def test_a_database_failure_fails_startup_and_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    models = _models_dir(settings)
    models.mkdir(parents=True)
    model_id = _seed(settings, ModelStatus.DOWNLOADING, models / "x.gguf")

    def fails(self: ModelRegistry, *args: Any, **kwargs: Any) -> bool:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ModelRegistry, "mark_interrupted", fails)
    with pytest.raises(sqlite3.OperationalError), TestClient(create_app(settings), client=LOOPBACK):
        pass
    monkeypatch.undo()
    # Not claimed as recovered; a later startup (lock released) recovers it.
    with TestClient(create_app(settings), client=LOOPBACK) as client:
        body = client.get(f"/admin/models/{model_id}").json()
    assert body["status"] == "error" and body["error"] == INTERRUPTED


def test_a_second_engine_on_the_same_store_refuses_to_start(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    with TestClient(create_app(settings), client=LOOPBACK):
        with pytest.raises(StoreLockedError), TestClient(create_app(settings), client=LOOPBACK):
            pass
    with TestClient(create_app(settings), client=LOOPBACK):  # released on shutdown
        pass
