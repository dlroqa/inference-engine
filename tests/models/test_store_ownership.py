"""One engine per writable store: its database AND its managed model directory.

Real OS locks throughout. Cross-process cases use a child engine process
(``tests.support.download_child``) holding both locks mid-download; it is always
killed in cleanup. Every wait is bounded; no sleeps decide a race.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
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
from engine.store.db import connect
from engine.store.migrations import apply_migrations
from engine.store.ownership import (
    MODEL_STORE_LOCK_NAME,
    StoreLock,
    StoreLockedError,
    StoreLockUnavailableError,
    StoreOwnership,
    lock_path_for,
    model_store_lock_path,
)
from engine.telemetry.service import Telemetry
from tests.models.test_download_worker_lifetime import A, B, GatedResponse, _serve

REPO = Path(__file__).resolve().parents[2]
LOOPBACK = ("127.0.0.1", 40000)
WAIT = 30


def _settings(tmp_path: Path, db: str, models: str) -> Settings:
    return Settings(data_dir=tmp_path / "data", db_path=tmp_path / db, models_dir=tmp_path / models)


def _app(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), client=LOOPBACK)


def _free(settings: Settings) -> bool:
    """Whether both of the store's locks can be taken right now (then released)."""
    probe = StoreOwnership(settings.db_path, settings.models_dir)  # type: ignore[arg-type]
    try:
        probe.acquire()
    except StoreLockedError:
        return False
    probe.release()
    return True


# -- in-process matrix ---------------------------------------------------------


def test_same_database_different_model_directories_is_refused(tmp_path: Path) -> None:
    owner = _settings(tmp_path, "shared.db", "models-a")
    other = _settings(tmp_path, "shared.db", "models-b")
    with _app(owner):
        with pytest.raises(StoreLockedError, match="owns this database"), _app(other):
            pass


def _seed_downloading(settings: Settings) -> str:
    conn = connect(settings.db_path)  # type: ignore[arg-type]
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    record = ModelRegistry(settings.db_path).create(  # type: ignore[arg-type]
        name="pending",
        filename="pending.gguf",
        path=Path(settings.models_dir) / "pending.gguf",  # type: ignore[arg-type]
        source_type="url",
        source_ref="address not stored",
        status=ModelStatus.DOWNLOADING,
    )
    return record.id


def _db_snapshot(settings: Settings) -> Any:
    conn = sqlite3.connect(str(settings.db_path))
    try:
        return (
            conn.execute("SELECT * FROM models").fetchall(),
            conn.execute("SELECT COUNT(*) FROM log_events").fetchone(),
        )
    finally:
        conn.close()


def test_different_databases_sharing_a_model_directory_is_refused(tmp_path: Path) -> None:
    owner = _settings(tmp_path, "a.db", "shared-models")
    other = _settings(tmp_path, "b.db", "shared-models")
    _seed_downloading(other)  # recovery would mark this interrupted if it ran
    before = _db_snapshot(other)
    with _app(owner):
        with pytest.raises(StoreLockedError, match="owns this model directory"), _app(other):
            pass
        # The loser ran no recovery and wrote nothing (not even log rows).
        assert _db_snapshot(other) == before
    # Its database lock was released when the second lock failed.
    probe = StoreLock(lock_path_for(other.db_path))  # type: ignore[arg-type]
    probe.acquire()
    probe.release()


def test_separate_stores_run_concurrently(tmp_path: Path) -> None:
    one = _settings(tmp_path, "a.db", "models-a")
    two = _settings(tmp_path, "b.db", "models-b")
    with _app(one) as a, _app(two) as b:
        assert a.get("/admin/models").status_code == 200
        assert b.get("/admin/models").status_code == 200


def test_relative_and_absolute_paths_contend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    owner = _settings(tmp_path, "a.db", "models")
    other = Settings(
        data_dir=Path("data2"), db_path=Path("b.db"), models_dir=Path("sub") / ".." / "models"
    )
    with _app(owner):
        with pytest.raises(StoreLockedError, match="owns this model directory"), _app(other):
            pass


def test_a_symlinked_model_directory_contends(tmp_path: Path) -> None:
    real = tmp_path / "models"
    real.mkdir()
    link = tmp_path / "models-link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks are not available here: {type(exc).__name__}")
    owner = _settings(tmp_path, "a.db", "models")
    other = _settings(tmp_path, "b.db", "models-link")
    with _app(owner):
        with pytest.raises(StoreLockedError, match="owns this model directory"), _app(other):
            pass


def test_second_lock_contention_releases_the_first(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "a.db", "models")
    holder = StoreLock(model_store_lock_path(settings.models_dir))  # type: ignore[arg-type]
    holder.acquire()
    try:
        with pytest.raises(StoreLockedError), _app(settings):
            pass
        database = StoreLock(lock_path_for(settings.db_path))  # type: ignore[arg-type]
        database.acquire()  # released by the failed start
        database.release()
    finally:
        holder.release()
    with _app(settings):
        pass


def test_an_unopenable_lock_file_is_not_reported_as_another_engine(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "a.db", "models")
    # A directory where the lock file should be: it cannot be opened as a file.
    model_store_lock_path(settings.models_dir).mkdir(parents=True)  # type: ignore[arg-type]
    with pytest.raises(StoreLockUnavailableError) as raised, _app(settings):
        pass
    assert not isinstance(raised.value, StoreLockedError)
    database = StoreLock(lock_path_for(settings.db_path))  # type: ignore[arg-type]
    database.acquire()  # the database lock was released
    database.release()


def test_both_locks_mapping_to_one_file_is_rejected(tmp_path: Path) -> None:
    models = tmp_path / "models"
    db = models / MODEL_STORE_LOCK_NAME.removesuffix(".lock")
    with pytest.raises(StoreLockUnavailableError, match="same lock file"):
        StoreOwnership(db, models)


def test_normal_shutdown_releases_both_and_lock_files_are_harmless(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "a.db", "models")
    with _app(settings) as client:
        assert client.get("/admin/models").json()["models"] == []
    assert lock_path_for(settings.db_path).exists()  # type: ignore[arg-type]
    assert model_store_lock_path(settings.models_dir).exists()  # type: ignore[arg-type]
    assert _free(settings)
    with _app(settings) as client:  # restart with the old (unlocked) files present
        assert client.get("/admin/models").json()["models"] == []


def test_a_failure_after_both_locks_releases_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, "a.db", "models")
    _seed_downloading(settings)

    def fails(self: ModelRegistry, *args: Any, **kwargs: Any) -> bool:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ModelRegistry, "mark_interrupted", fails)
    with pytest.raises(sqlite3.OperationalError), _app(settings):
        pass
    assert _free(settings)


# -- shutdown keeps ownership while a worker can still write ------------------


def _start_blocked_download(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> GatedResponse:
    response = GatedResponse([A, B], block_at=1)
    _serve(monkeypatch, response)
    body = {"source_type": "url", "url": "https://cdn.example.com/m.gguf", "filename": "m.gguf"}
    assert client.post("/admin/models/download", json=body).status_code == 200
    assert response.blocked.wait(WAIT)
    return response


@pytest.mark.parametrize("cleanup", ["clean", "unrelated-step-fails"])
def test_competitors_are_refused_until_the_worker_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup: str
) -> None:
    settings = _settings(tmp_path, "a.db", "models")
    if cleanup == "unrelated-step-fails":

        async def broken_stop(self: Telemetry) -> None:
            raise RuntimeError("telemetry stop failed")

        monkeypatch.setattr(Telemetry, "stop", broken_stop)
    client = _app(settings)
    client.__enter__()
    response = _start_blocked_download(client, monkeypatch)
    outcome: dict[str, Any] = {}

    def shut_down() -> None:
        try:
            client.__exit__(None, None, None)
        except BaseException as exc:
            outcome["error"] = exc

    stopper = threading.Thread(target=shut_down, daemon=True)
    try:
        stopper.start()
        # Shutdown is waiting for the blocked worker: the store is still owned.
        stopper.join(0.5)
        assert stopper.is_alive(), "shutdown finished while the worker was blocked"
        assert not _free(settings), "ownership was released while a worker could write"
    finally:
        response.release.set()
    stopper.join(WAIT)
    assert not stopper.is_alive()
    if cleanup == "unrelated-step-fails":
        assert isinstance(outcome.get("error"), RuntimeError)
    else:
        assert "error" not in outcome
    assert _free(settings)  # released only after the worker stopped
    assert response.closed.is_set()


def test_cancelling_shutdown_keeps_ownership_until_the_worker_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, "a.db", "models")
    response = GatedResponse([A, B], block_at=1)
    _serve(monkeypatch, response)
    app = create_app(settings)
    stop = asyncio.Event()

    async def run() -> None:
        async with app.router.lifespan_context(app):
            app.state.model_service.start_download(
                source_type="url", url="https://cdn.example.com/m.gguf", filename="m.gguf"
            )
            await stop.wait()

    async def main() -> None:
        lifespan = asyncio.create_task(run())
        try:
            assert await asyncio.to_thread(response.blocked.wait, WAIT)
            stop.set()
            await asyncio.sleep(0)  # let shutdown begin
            lifespan.cancel()
            await asyncio.sleep(0.05)
            lifespan.cancel()  # repeated cancellation
            done, _ = await asyncio.wait({lifespan}, timeout=0.3)
            assert not done, "shutdown returned while the worker was blocked"
            assert not await asyncio.to_thread(_free, settings)
        finally:
            response.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(lifespan), WAIT)

    asyncio.run(main())
    assert _free(settings)


# -- cross-process -------------------------------------------------------------


@contextmanager
def _child(settings: Settings, barrier: str) -> Iterator[subprocess.Popen[bytes]]:
    marker = Path(settings.data_dir).parent / f"{barrier}.reached"
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    args = [str(settings.data_dir), barrier, str(marker), str(settings.db_path)]
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.support.download_child", *args],
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
        proc.kill()
        proc.wait(timeout=WAIT)
        if proc.stderr:
            proc.stderr.close()


@pytest.mark.parametrize("barrier", ["read", "finalize"])
def test_a_child_engine_owns_both_resources_until_killed(tmp_path: Path, barrier: str) -> None:
    owner = Settings(data_dir=tmp_path / "data")
    same_models = Settings(
        data_dir=tmp_path / "other", db_path=tmp_path / "other.db", models_dir=owner.models_dir
    )
    same_db = Settings(
        data_dir=tmp_path / "third", db_path=owner.db_path, models_dir=tmp_path / "models-c"
    )
    with _child(owner, barrier):
        with pytest.raises(StoreLockedError, match="owns this model directory"), _app(same_models):
            pass
        with pytest.raises(StoreLockedError, match="owns this database"), _app(same_db):
            pass
    # Killed: both locks are reusable, and the existing conservative recovery runs.
    assert _free(owner)
    with _app(owner) as client:
        [model] = client.get("/admin/models").json()["models"]
        assert model["status"] == "error" and model["loaded"] is False
        assert client.delete(f"/admin/models/{model['id']}").status_code == 200
        assert client.get("/admin/models").json()["models"] == []
    # The reserved lock file survives delete and recovery, and never became a model.
    assert model_store_lock_path(owner.models_dir).exists()  # type: ignore[arg-type]
    left = [p.name for p in Path(owner.models_dir).iterdir()]  # type: ignore[arg-type]
    assert left == [MODEL_STORE_LOCK_NAME]
