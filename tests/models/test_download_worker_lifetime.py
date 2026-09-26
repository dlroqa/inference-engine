"""Download worker lifetime: cancellation never lets a worker write or promote late.

The real downloader runs against a controlled fake response whose ``read()``
can block on an event and whose ``close()`` is observable, so its own file I/O,
cancellation checkpoints and commit point are what is tested. Blocked reads
are always released in ``finally`` and every wait is bounded. No network.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import logging
import threading
import urllib.request
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from engine.config import Settings
from engine.logging_setup import JsonFormatter
from engine.models import downloader, gguf
from engine.models.registry import ModelRegistry, ModelStatus
from engine.models.service import ModelService
from engine.store.db import connect
from engine.store.migrations import apply_migrations

A = b"A" * 8
B = b"B" * 8
C = b"C" * 8
URL = "https://cdn.example.com/m.gguf"
WAIT = 10  # seconds; every blocking wait in these tests is bounded


class GatedResponse:
    """A fake HTTP response for the real downloader. One read can block."""

    status = 200

    def __init__(
        self,
        chunks: list[bytes],
        *,
        block_at: int | None = None,
        fail_with: BaseException | None = None,
    ) -> None:
        self._chunks = chunks
        self.headers = {"Content-Length": str(sum(len(c) for c in chunks))}
        self.block_at = block_at
        self.fail_with = fail_with
        self.reads = 0
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()

    def read(self, size: int) -> bytes:
        index = self.reads
        self.reads += 1
        if index == self.block_at:
            self.blocked.set()
            if not self.release.wait(WAIT):
                raise OSError("test response was never released")
            if self.fail_with is not None:
                raise self.fail_with
        return self._chunks[index] if index < len(self._chunks) else b""

    def close(self) -> None:
        self.closed.set()


def _serve(monkeypatch: pytest.MonkeyPatch, response: GatedResponse) -> None:
    def urlopen(*args: Any, **kwargs: Any) -> GatedResponse:
        return response

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


class _Hasher:
    """Real SHA-256 with a hook, to request cancellation at a precise moment."""

    def __init__(
        self,
        on_update: Callable[[int], None] | None = None,
        on_digest: Callable[[], None] | None = None,
    ) -> None:
        self._h = hashlib.sha256()
        self.updates = 0
        self._on_update = on_update
        self._on_digest = on_digest

    def update(self, data: bytes) -> None:
        self._h.update(data)
        self.updates += 1
        if self._on_update is not None:
            self._on_update(self.updates)

    def hexdigest(self) -> str:
        if self._on_digest is not None:
            self._on_digest()
        return self._h.hexdigest()


def _in_thread(fn: Callable[[], str]) -> tuple[threading.Thread, dict[str, Any]]:
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = fn()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _part(dest: Path) -> Path:
    return dest.with_suffix(dest.suffix + ".part")


# -- the real downloader ------------------------------------------------------


@pytest.mark.parametrize(("chunks", "released"), [([A, B], "bytes"), ([A], "EOF")])
def test_cancel_during_blocked_read_discards_what_the_read_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chunks: list[bytes], released: str
) -> None:
    response = GatedResponse(chunks, block_at=1)
    _serve(monkeypatch, response)
    dest = tmp_path / "m.gguf"
    cancel = downloader.CancelEvent()
    thread, outcome = _in_thread(lambda: downloader.download(URL, dest, cancel=cancel))
    try:
        assert response.blocked.wait(WAIT)
        assert cancel.request()
        assert thread.is_alive() and not response.closed.is_set() and not dest.exists()
    finally:
        response.release.set()
    thread.join(WAIT)

    assert not thread.is_alive(), "the worker did not finish"
    assert isinstance(outcome.get("error"), downloader.DownloadCancelled), released
    assert _part(dest).read_bytes() == A, f"{released}: data was accepted after cancel"
    assert not dest.exists(), f"{released}: the file was promoted"
    assert response.closed.is_set()
    assert not cancel.committed


def test_cancel_during_checksum_stops_the_real_checksum_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, response := GatedResponse([A, B, C]))
    dest = tmp_path / "m.gguf"
    cancel = downloader.CancelEvent()
    hasher = _Hasher(on_update=lambda n: cancel.request() if n == 1 else None)
    monkeypatch.setattr(downloader, "hashlib", SimpleNamespace(sha256=lambda: hasher))

    with pytest.raises(downloader.DownloadCancelled):
        downloader.download(URL, dest, cancel=cancel, chunk_bytes=len(A))

    assert hasher.updates == 1, "the checksum loop kept reading after cancellation"
    assert _part(dest).read_bytes() == A + B + C, "the partial file was not kept"
    assert not dest.exists()
    assert response.closed.is_set()


def test_cancel_accepted_before_the_commit_point_prevents_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, GatedResponse([A, B]))
    dest = tmp_path / "m.gguf"
    cancel = downloader.CancelEvent()
    # The checksum is complete; cancellation is requested just before promotion.
    hasher = _Hasher(on_digest=lambda: cancel.request() or None)
    monkeypatch.setattr(downloader, "hashlib", SimpleNamespace(sha256=lambda: hasher))

    with pytest.raises(downloader.DownloadCancelled):
        downloader.download(URL, dest, cancel=cancel)

    assert not dest.exists()
    assert _part(dest).read_bytes() == A + B
    assert cancel.is_set() and not cancel.committed


def test_completion_at_the_commit_point_wins_and_later_cancels_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, response := GatedResponse([A, B]))
    dest = tmp_path / "m.gguf"
    cancel = downloader.CancelEvent()

    digest = downloader.download(URL, dest, cancel=cancel)

    assert digest == hashlib.sha256(A + B).hexdigest()
    assert dest.read_bytes() == A + B and not _part(dest).exists()
    assert response.closed.is_set()
    assert cancel.committed
    assert cancel.request() is False and not cancel.is_set()


def test_a_plain_event_still_cancels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    response = GatedResponse([A, B], block_at=1)
    _serve(monkeypatch, response)
    dest = tmp_path / "m.gguf"
    cancel = threading.Event()
    thread, outcome = _in_thread(lambda: downloader.download(URL, dest, cancel=cancel))
    try:
        assert response.blocked.wait(WAIT)
        cancel.set()
    finally:
        response.release.set()
    thread.join(WAIT)
    assert isinstance(outcome.get("error"), downloader.DownloadCancelled)
    assert _part(dest).read_bytes() == A and not dest.exists()


# -- the service owns its worker ----------------------------------------------


@pytest.fixture
def service(tmp_path: Path) -> ModelService:
    db = tmp_path / "ie.db"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return ModelService(Settings(data_dir=tmp_path / "data"), ModelRegistry(db))


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    service_logger = logging.getLogger("engine.models")
    old_level = service_logger.level
    service_logger.setLevel(logging.DEBUG)
    root = logging.getLogger()
    handler = _Capture(level=logging.DEBUG)
    root.addHandler(handler)
    try:
        yield captured
    finally:
        root.removeHandler(handler)
        service_logger.setLevel(old_level)


def _run(go: Callable[[], Awaitable[str]]) -> tuple[str, list[dict[str, Any]]]:
    """Runs ``go`` on a fresh loop with an isolated exception collector."""
    loop_errors: list[dict[str, Any]] = []

    async def main() -> str:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        result = await go()
        for _ in range(3):
            await asyncio.sleep(0)
        gc.collect()
        return result

    result = asyncio.run(main())
    gc.collect()
    return result, loop_errors


def _events(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if r.name == "engine.models"]


def _status(service: ModelService, model_id: str) -> str:
    stored = service.registry.get(model_id)
    assert stored is not None
    return stored.status


def _dest(service: ModelService) -> Path:
    return service.models_dir / "m.gguf"


def test_cancel_request_during_blocked_read_records_cancelled_after_the_worker_stops(
    service: ModelService, records: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    response = GatedResponse([A, B], block_at=1)
    _serve(monkeypatch, response)

    async def go() -> str:
        model_id = service.start_download(source_type="url", url=URL).id
        task = service._tasks[model_id]
        try:
            assert await asyncio.to_thread(response.blocked.wait, WAIT)
            assert service.cancel(model_id)
            done, _ = await asyncio.wait({task}, timeout=0.3)
            assert not done and _status(service, model_id) == ModelStatus.DOWNLOADING.value
        finally:
            response.release.set()
        await asyncio.wait_for(task, WAIT)
        return model_id

    model_id, loop_errors = _run(go)
    assert loop_errors == []
    assert _status(service, model_id) == ModelStatus.CANCELLED.value
    stored = service.registry.get(model_id)
    assert stored is not None and stored.downloaded_bytes == len(A)
    assert _part(_dest(service)).read_bytes() == A and not _dest(service).exists()
    assert response.closed.is_set()
    assert "model_download_failed" not in _events(records)
    assert service._tasks == {} and service._cancels == {}


def test_shutdown_grace_expiry_keeps_ownership_until_the_worker_stops(
    service: ModelService, records: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    response = GatedResponse([A, B], block_at=1)
    _serve(monkeypatch, response)

    async def go() -> str:
        model_id = service.start_download(source_type="url", url=URL).id
        try:
            assert await asyncio.to_thread(response.blocked.wait, WAIT)
            shutdown = asyncio.create_task(service.shutdown(grace_seconds=0.05))
            done, _ = await asyncio.wait({shutdown}, timeout=0.5)
            # Grace expired, but the worker is blocked: shutdown has not finished,
            # ownership is kept, and cancellation is not reported yet.
            assert not done, "shutdown returned while the worker was running"
            assert "model_download_shutdown_waiting" in _events(records)
            assert model_id in service._tasks and service._cancels[model_id].is_set()
            assert _status(service, model_id) == ModelStatus.DOWNLOADING.value
            assert not response.closed.is_set()
        finally:
            response.release.set()
        await asyncio.wait_for(shutdown, WAIT)
        return model_id

    model_id, loop_errors = _run(go)
    assert loop_errors == []
    assert response.closed.is_set()
    assert _status(service, model_id) == ModelStatus.CANCELLED.value
    assert _part(_dest(service)).read_bytes() == A and not _dest(service).exists()
    assert service._tasks == {} and service._cancels == {}

    async def again() -> str:
        await service.shutdown(grace_seconds=0.05)
        return "done"

    assert _run(again) == ("done", [])  # a repeated shutdown is a safe no-op


def test_repeated_task_cancellation_keeps_the_worker_owned(
    service: ModelService, records: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    response = GatedResponse([A, B], block_at=1)
    _serve(monkeypatch, response)

    async def go() -> str:
        model_id = service.start_download(source_type="url", url=URL).id
        task = service._tasks[model_id]
        try:
            assert await asyncio.to_thread(response.blocked.wait, WAIT)
            task.cancel()
            await asyncio.sleep(0.05)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=0.3)
            assert not done, "the task finished while its worker was running"
            assert service._cancels[model_id].is_set()
            assert _status(service, model_id) == ModelStatus.DOWNLOADING.value
        finally:
            response.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), WAIT)
        assert task.cancelled()
        return model_id

    model_id, loop_errors = _run(go)
    assert loop_errors == []
    assert _status(service, model_id) == ModelStatus.CANCELLED.value
    assert _part(_dest(service)).read_bytes() == A and not _dest(service).exists()
    assert response.closed.is_set()
    assert _events(records) == ["model_download_cancelled"]
    assert service._tasks == {} and service._cancels == {}


def test_late_worker_failure_after_cancellation_is_contained(
    service: ModelService, records: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = OSError("reset by https://u:synthetic-pw-301@h.example/m?token=synthetic-tok-301")
    response = GatedResponse([A, B], block_at=1, fail_with=failure)
    _serve(monkeypatch, response)

    async def go() -> str:
        model_id = service.start_download(source_type="url", url=URL).id
        task = service._tasks[model_id]
        try:
            assert await asyncio.to_thread(response.blocked.wait, WAIT)
            task.cancel()
        finally:
            response.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), WAIT)
        return model_id

    model_id, loop_errors = _run(go)
    assert loop_errors == [], "the late failure escaped the task"
    # The worker's real outcome is recorded: it failed before it saw the cancel.
    assert _status(service, model_id) == ModelStatus.ERROR.value
    assert "model_download_failed" in _events(records)
    for record in records:
        line = JsonFormatter().format(record)
        assert "synthetic" not in line and "synthetic" not in repr(vars(record))
    assert response.closed.is_set()
    assert service._tasks == {} and service._cancels == {}


def _blocking_probe(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    entered, release = threading.Event(), threading.Event()
    real_probe = gguf.probe

    def probe(path: Path) -> gguf.GgufInfo:
        entered.set()
        if not release.wait(WAIT):
            raise OSError("test probe was never released")
        return real_probe(path)

    monkeypatch.setattr(gguf, "probe", probe)
    return entered, release


def test_shutdown_during_finalize_waits_and_the_committed_download_completes(
    service: ModelService, records: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, response := GatedResponse([A, B]))
    entered, release = _blocking_probe(monkeypatch)

    async def go() -> str:
        model_id = service.start_download(source_type="url", url=URL).id
        try:
            assert await asyncio.to_thread(entered.wait, WAIT)
            assert _dest(service).read_bytes() == A + B  # committed
            shutdown = asyncio.create_task(service.shutdown(grace_seconds=0.05))
            done, _ = await asyncio.wait({shutdown}, timeout=0.3)
            assert not done, "shutdown returned during finalization"
            assert model_id in service._tasks
            assert _status(service, model_id) == ModelStatus.DOWNLOADING.value
        finally:
            release.set()
        await asyncio.wait_for(shutdown, WAIT)
        return model_id

    model_id, loop_errors = _run(go)
    assert loop_errors == []
    assert _status(service, model_id) == ModelStatus.READY.value
    assert "model_download_cancelled" not in _events(records)
    assert "model_download_ready" in _events(records)
    assert response.closed.is_set()
    assert service._tasks == {} and service._cancels == {}


def test_cancel_after_the_commit_point_is_refused_and_the_download_completes(
    service: ModelService, records: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, GatedResponse([A, B]))
    entered, release = _blocking_probe(monkeypatch)

    async def go() -> str:
        model_id = service.start_download(source_type="url", url=URL).id
        task = service._tasks[model_id]
        try:
            assert await asyncio.to_thread(entered.wait, WAIT)
            assert service.cancel(model_id) is False
        finally:
            release.set()
        await asyncio.wait_for(task, WAIT)
        return model_id

    model_id, loop_errors = _run(go)
    assert loop_errors == []
    assert _status(service, model_id) == ModelStatus.READY.value
    stored = service.registry.get(model_id)
    assert stored is not None and stored.sha256 == hashlib.sha256(A + B).hexdigest()
    assert "model_download_cancelled" not in _events(records)
    assert service._tasks == {} and service._cancels == {}
