"""Download-failure log lines never carry credentials from the failure text.

The downloader is mocked to fail deterministically (or, for invalid URLs, runs
for real with network access refused); the real service failure path runs.
Boundary tests start downloads fire-and-forget and watch the event loop's
exception handler, so an exception escaping the task would be seen. Each
emitted ``LogRecord`` is inspected before any formatting (other handlers see
the raw record), then formatted with the real ``JsonFormatter``. All secrets
are synthetic; assertions report only which case failed, never the payload.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from engine.config import Settings
from engine.logging_setup import JsonFormatter
from engine.models import downloader, gguf, redaction
from engine.models import service as service_module
from engine.models.registry import ModelRegistry, ModelStatus
from engine.models.service import ModelService
from engine.store.db import connect
from engine.store.migrations import apply_migrations

# Each case: the failure text, and a fragment the safe detail must still contain.
CASES: dict[str, tuple[str, str]] = {
    # URL userinfo is outside the formatter's own query-name policy.
    "userinfo": (
        "network error fetching model: https://svc:synthetic-pw-101@cdn.example.com/m.gguf failed",
        "https://cdn.example.com/m.gguf failed",
    ),
    "signed redirect URL": (
        "redirected to https://s3.example.com/b/m.gguf?X-Amz-Credential=synthetic-cred-102"
        "&X-Amz-Signature=synthetic-sig-102&X-Amz-Date=20260926",
        "X-Amz-Date=20260926",
    ),
    "scheme-less target with an encoded name": (
        "URL can't contain control characters. '/m.gguf?%74oken=synthetic-tok-103 x' "
        "(found at least ' ')",
        "?%74oken=***",
    ),
    "multiple URLs": (
        "tried http://u:synthetic-pw-104@a.example/m.gguf "
        "then HTTPS://b.example/m.gguf?sig=synthetic-sig-104",
        "a.example/m.gguf",
    ),
    "encoded and unlisted names": (
        "rejected ?Api%5FKey=synthetic-key-105&auth=synthetic-auth-105&lang=en",
        "&lang=en",
    ),
    "bracketed IPv6 and punctuation": (
        "fetching https://u:synthetic-pw-106@[::1]:8443/m.gguf?%73ig=synthetic-sig-106.",
        "https://[::1]:8443/m.gguf?sig=***.",
    ),
    "malformed port": (
        "bad URL https://u:synthetic-pw-107@cdn.example.com:notaport/m.gguf"
        "?token=synthetic-tok-107",
        "cdn.example.com:notaport/m.gguf?***",
    ),
    "parentheses in userinfo": (
        "fetching https://u:synthetic(pw)-108@cdn.example.com/m.gguf",
        "https://cdn.example.com/m.gguf",
    ),
    "quote in a credential value": (
        "rejected '/m.gguf?token=synthetic'tok-109' (bad)",
        "?token=***' (bad)",
    ),
    "nested redirect target": (
        "redirect https://cdn.example.com/go?next=https://s3.example.com/m?token=synthetic-tok-110",
        "cdn.example.com/go",
    ),
    "encoded nested redirect target": (
        "redirect https://cdn.example.com/go?"
        "next=https%3A%2F%2Fs3.example.com%2Fm%3Fsig%3Dsynthetic-sig-111",
        "cdn.example.com/go",
    ),
    "parentheses in a credential value": (
        "HTTP 403 fetching https://h.example/m?token=synthetic(tok)-112 (denied)",
        "(denied)",
    ),
}
SECRET_MARKERS = ("synthetic-", "synthetic(", "synthetic'", "tok)-112", "(pw)-108", "tok-109")

USEFUL = (
    "HTTP 404 fetching model",
    "checksum mismatch: expected aaaa, got bbbb",
    "network error fetching model: <urlopen error [Errno -2] Name or service not known>",
    "model exceeds max_model_bytes (10 > 5)",
)


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    """Every record the service logger emits, unformatted."""
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    logger = logging.getLogger("engine.models")
    handler = _Capture(level=logging.DEBUG)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield captured
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.fixture
def all_records() -> Iterator[list[logging.LogRecord]]:
    """Every record reaching the root logger (service, asyncio, anything else)."""
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    root = logging.getLogger()
    handler = _Capture(level=logging.DEBUG)
    root.addHandler(handler)
    try:
        yield captured
    finally:
        root.removeHandler(handler)


@pytest.fixture
def service(tmp_path: Path) -> ModelService:
    settings = Settings(data_dir=tmp_path / "data")
    db = tmp_path / "ie.db"
    conn = connect(db)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return ModelService(settings, ModelRegistry(db))


def _failing(exc: BaseException) -> Callable[..., str]:
    def fake(*args: Any, **kwargs: Any) -> str:
        raise exc

    return fake


def _run(service: ModelService, url: str = "https://cdn.example.com/m.gguf") -> str:
    """Starts a download through the service and awaits the task to completion."""

    async def go() -> str:
        record = service.start_download(source_type="url", url=url)
        await service._tasks[record.id]
        return record.id

    return asyncio.run(go())


def _failures(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.getMessage() == "model_download_failed"]


def _record_leaks(record: logging.LogRecord) -> bool:
    fields = [record.getMessage(), repr(record.args), repr(record.msg)]
    fields += [repr(v) for v in vars(record).values()]
    return any(m in f for f in fields for m in SECRET_MARKERS)


def _output_leaks(line: str) -> bool:
    payload = json.loads(line)
    values = [line, *(str(v) for v in payload.values())]
    return any(m in v for v in values for m in SECRET_MARKERS)


@pytest.mark.parametrize("case", sorted(CASES))
def test_failure_record_and_json_output_carry_no_credentials(
    service: ModelService,
    records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    # Flagged safe, as the downloader does for the messages it builds: the log
    # line still passes through the shared redaction (defense in depth).
    text, kept = CASES[case]
    exc = downloader.DownloadError(text, safe=True)
    monkeypatch.setattr(downloader, "download", _failing(exc))
    model_id = _run(service)

    [record] = _failures(records)
    assert record.levelno == logging.WARNING
    assert record.model_id == model_id  # type: ignore[attr-defined]
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    assert not _record_leaks(record), f"{case}: a credential reached the LogRecord"
    assert kept in record.detail, f"{case}: useful detail was lost"  # type: ignore[attr-defined]

    line = JsonFormatter().format(record)
    assert not _output_leaks(line), f"{case}: a credential reached the JSON output"
    payload = json.loads(line)
    assert payload["event"] == "model_download_failed"
    assert payload["level"] == "WARNING"
    assert payload["model_id"] == model_id
    assert kept in payload["detail"]

    # Lifecycle: error state, and the task is no longer tracked.
    stored = service.registry.get(model_id)
    assert stored is not None
    assert stored.status == ModelStatus.ERROR.value
    assert model_id not in service._tasks and model_id not in service._cancels


@pytest.mark.parametrize("case", sorted(CASES))
def test_unclassified_failure_text_is_never_stored_or_logged(
    service: ModelService,
    records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    text, _ = CASES[case]
    monkeypatch.setattr(downloader, "download", _failing(downloader.DownloadError(text)))
    model_id = _run(service)

    label = "download failed during download (DownloadError)"
    [record] = _failures(records)
    assert record.detail == label  # type: ignore[attr-defined]
    assert not _record_leaks(record), f"{case}: failure text reached the LogRecord"
    stored = service.registry.get(model_id)
    assert stored is not None and stored.error == label
    assert not any(m in repr(stored) for m in SECRET_MARKERS), f"{case}: stored"


@pytest.mark.parametrize("text", USEFUL)
def test_non_sensitive_failures_stay_useful(
    service: ModelService,
    records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    exc = (
        downloader.ChecksumMismatch(text, safe=True)
        if text.startswith("checksum")
        else downloader.DownloadError(text, safe=True)
    )
    monkeypatch.setattr(downloader, "download", _failing(exc))
    model_id = _run(service)
    [record] = _failures(records)
    assert record.detail == text  # type: ignore[attr-defined]
    assert json.loads(JsonFormatter().format(record))["detail"] == text
    stored = service.registry.get(model_id)
    assert stored is not None and stored.status == ModelStatus.ERROR.value


def test_redaction_failure_logs_a_neutral_detail(
    service: ModelService,
    records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(text: str | None) -> str | None:
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr(service_module, "redact_urls_in_text", broken)
    text, _ = CASES["userinfo"]
    monkeypatch.setattr(downloader, "download", _failing(downloader.DownloadError(text)))
    model_id = _run(service)

    [record] = _failures(records)
    assert record.detail == redaction.WITHHELD  # type: ignore[attr-defined]
    assert not _record_leaks(record), "fallback: a credential reached the LogRecord"
    assert not _output_leaks(JsonFormatter().format(record)), "fallback: leaked in output"
    # Nothing else was logged about the failure.
    assert [r.getMessage() for r in records] == ["model_download_failed"]
    stored = service.registry.get(model_id)
    assert stored is not None and stored.status == ModelStatus.ERROR.value
    assert model_id not in service._tasks and model_id not in service._cancels


def test_unconvertible_exception_logs_a_neutral_detail(
    service: ModelService,
    records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Unprintable(downloader.DownloadError):
        def __str__(self) -> str:
            raise RuntimeError("cannot render")

    monkeypatch.setattr(downloader, "download", _failing(Unprintable("x", safe=True)))
    model_id = _run(service)
    [record] = _failures(records)
    assert record.detail == redaction.WITHHELD  # type: ignore[attr-defined]
    stored = service.registry.get(model_id)
    assert stored is not None
    assert stored.status == ModelStatus.ERROR.value
    assert stored.error == redaction.WITHHELD
    assert model_id not in service._tasks and model_id not in service._cancels


def test_cancelled_download_is_not_logged_as_a_failure(
    service: ModelService,
    records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()

    def blocks_until_cancelled(*args: Any, cancel: threading.Event, **kwargs: Any) -> str:
        started.set()
        cancel.wait(timeout=10)
        raise downloader.DownloadCancelled("download cancelled")

    monkeypatch.setattr(downloader, "download", blocks_until_cancelled)

    async def go() -> str:
        record = service.start_download(source_type="url", url="https://cdn.example.com/m.gguf")
        task = service._tasks[record.id]
        await asyncio.to_thread(started.wait, 10)
        assert service.cancel(record.id)
        await task
        return record.id

    model_id = asyncio.run(go())
    assert _failures(records) == []
    assert [r.getMessage() for r in records] == ["model_download_cancelled"]
    stored = service.registry.get(model_id)
    assert stored is not None and stored.status == ModelStatus.CANCELLED.value
    assert model_id not in service._tasks and model_id not in service._cancels


def test_service_does_not_depend_on_the_api_layer() -> None:
    root = Path(__file__).resolve().parents[2] / "engine" / "models"
    for name in ("redaction.py", "service.py"):
        source = (root / name).read_text()
        assert "engine.api" not in source, f"{name} imports the API layer"
    redaction_source = (root / "redaction.py").read_text()
    for forbidden in ("fastapi", "engine.logging_setup", "engine.models.registry"):
        assert forbidden not in redaction_source


def test_api_helpers_are_the_shared_redactor() -> None:
    from engine.api import models_router

    assert models_router.redact_source_ref is redaction.redact_source_ref
    assert models_router.redact_urls_in_text is redaction.redact_urls_in_text


# -- the owned task's exception boundary ---------------------------------------
#
# These tests use the service's normal fire-and-forget start: nothing awaits the
# task or consumes its exception, so an exception escaping it would reach the
# event loop's unhandled-task reporting, which an isolated collector observes.


def _fire_and_forget(
    service: ModelService, url: str = "https://cdn.example.com/m.gguf"
) -> tuple[str, list[dict[str, Any]]]:
    """Starts a download without awaiting it; returns loop exception reports."""
    loop_errors: list[dict[str, Any]] = []

    async def go() -> str:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        model_id = service.start_download(source_type="url", url=url).id
        deadline = time.monotonic() + 10
        while model_id in service._tasks:
            assert time.monotonic() < deadline, "the download task did not finish"
            await asyncio.sleep(0.01)
        for _ in range(3):  # let the finished task be released and reported
            await asyncio.sleep(0)
        gc.collect()
        return model_id

    model_id = asyncio.run(go())
    gc.collect()
    return model_id, loop_errors


def _assert_safe(all_records: list[logging.LogRecord], label: str) -> None:
    for record in all_records:
        assert not _record_leaks(record), f"{label}: a credential reached a LogRecord"
        assert not _output_leaks(JsonFormatter().format(record)), f"{label}: leaked in JSON"


def test_loop_collector_detects_an_unhandled_task_exception() -> None:
    """Control: the harness does see a real unhandled, non-sensitive task failure."""
    loop_errors: list[dict[str, Any]] = []

    async def go() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

        async def fails() -> None:
            raise RuntimeError("control failure")

        task = asyncio.create_task(fails())
        for _ in range(3):
            await asyncio.sleep(0)
        del task
        gc.collect()

    asyncio.run(go())
    gc.collect()
    assert any("never retrieved" in str(c.get("message")) for c in loop_errors)


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("//u:synthetic-pw-201@host.invalid/m.gguf?token=synthetic-tok-201", "ValueError"),
        ("http://127.0.0.1:9/m.gguf?token=synthetic-tok-202 x", "InvalidURL"),
    ],
)
def test_real_invalid_url_exceptions_reach_the_safe_terminal_path(
    service: ModelService,
    records: list[logging.LogRecord],
    all_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    kind: str,
) -> None:
    attempts: list[object] = []

    def refuse(*args: object, **kwargs: object) -> socket.socket:
        attempts.append(args)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket, "create_connection", refuse)
    model_id, loop_errors = _fire_and_forget(service, url)  # the real downloader runs

    assert attempts == [], "a connection was attempted"
    assert loop_errors == [], f"{kind}: an exception escaped the download task"
    [record] = _failures(records)
    assert record.detail == f"invalid model URL ({kind})"  # type: ignore[attr-defined]
    assert record.stage == "download"  # type: ignore[attr-defined]
    _assert_safe(all_records, kind)
    stored = service.registry.get(model_id)
    assert stored is not None
    assert stored.status == ModelStatus.ERROR.value
    assert stored.error == f"invalid model URL ({kind})"
    assert model_id not in service._tasks and model_id not in service._cancels


def test_unexpected_worker_exception_is_contained(
    service: ModelService,
    records: list[logging.LogRecord],
    all_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "worker broke on https://u:synthetic-pw-204@h.example/m?token=synthetic-tok-204"
    monkeypatch.setattr(downloader, "download", _failing(RuntimeError(text)))
    model_id, loop_errors = _fire_and_forget(service)

    assert loop_errors == [], "an exception escaped the download task"
    [record] = _failures(records)
    assert record.stage == "download"  # type: ignore[attr-defined]
    assert record.error_type == "RuntimeError"  # type: ignore[attr-defined]
    # Unclassified exception text is never stored or logged: stage and type only.
    label = "download failed during download (RuntimeError)"
    assert record.detail == label  # type: ignore[attr-defined]
    _assert_safe(all_records, "unexpected worker exception")
    stored = service.registry.get(model_id)
    assert stored is not None and stored.status == ModelStatus.ERROR.value
    assert stored.error == label
    assert model_id not in service._tasks and model_id not in service._cancels


def test_post_download_failure_is_contained(
    service: ModelService,
    records: list[logging.LogRecord],
    all_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def completes(url: str, dest: Path, **kwargs: Any) -> str:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not a gguf")
        return "0" * 64

    def probe_fails(path: Path) -> object:
        raise ValueError("probe failed for https://h.example/m?sig=synthetic-sig-205")

    monkeypatch.setattr(downloader, "download", completes)
    monkeypatch.setattr(gguf, "probe", probe_fails)
    model_id, loop_errors = _fire_and_forget(service)

    assert loop_errors == [], "an exception escaped the download task"
    [record] = _failures(records)
    assert record.stage == "finalize"  # type: ignore[attr-defined]
    assert record.error_type == "ValueError"  # type: ignore[attr-defined]
    assert "model_download_ready" not in [r.getMessage() for r in records]
    _assert_safe(all_records, "post-download failure")
    stored = service.registry.get(model_id)
    assert stored is not None and stored.status == ModelStatus.ERROR.value
    assert model_id not in service._tasks and model_id not in service._cancels


def test_failure_to_persist_the_error_is_contained_and_reported(
    service: ModelService,
    records: list[logging.LogRecord],
    all_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def store_down(model_id: str, **fields: Any) -> None:
        calls.append(fields)
        raise sqlite3.OperationalError("disk I/O error synthetic-db-206")

    text, _ = CASES["userinfo"]
    monkeypatch.setattr(downloader, "download", _failing(downloader.DownloadError(text)))
    monkeypatch.setattr(service.registry, "update", store_down)
    model_id, loop_errors = _fire_and_forget(service)

    assert loop_errors == [], "an exception escaped the download task"
    assert len(calls) == 1, "the terminal update was not attempted exactly once"
    events = [r.getMessage() for r in records]
    assert events == ["model_download_state_not_recorded", "model_download_failed"]
    not_recorded = records[0]
    assert not_recorded.status == "error"  # type: ignore[attr-defined]
    assert not_recorded.error_type == "OperationalError"  # type: ignore[attr-defined]
    _assert_safe(all_records, "persistence failure")
    # Honest: the state change did not persist, and nothing claims it did.
    stored = service.registry.get(model_id)
    assert stored is not None and stored.status == ModelStatus.DOWNLOADING.value
    assert model_id not in service._tasks and model_id not in service._cancels


def _blocking_worker(
    started: threading.Event, stopped: threading.Event, release: threading.Event | None = None
) -> Callable[..., str]:
    """A fake worker that honours the cancel event, like the real one does between chunks."""

    def worker(
        *args: Any, cancel: threading.Event, progress_cb: Callable[..., None], **kwargs: Any
    ) -> str:
        started.set()
        try:
            if release is not None:  # ignores cancellation until released
                release.wait(timeout=10)
                progress_cb(123, 456)  # a late progress report
            cancel.wait(timeout=10)
            raise downloader.DownloadCancelled("download cancelled")
        finally:
            stopped.set()

    return worker


def test_task_cancellation_propagates_and_stops_the_worker(
    service: ModelService,
    records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, stopped = threading.Event(), threading.Event()
    monkeypatch.setattr(downloader, "download", _blocking_worker(started, stopped))

    async def go() -> str:
        model_id = service.start_download(source_type="url", url="https://h.example/m.gguf").id
        task = service._tasks[model_id]
        await asyncio.to_thread(started.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(stopped.wait, 10), "the worker kept running"
        return model_id

    model_id = asyncio.run(go())
    assert _failures(records) == []
    assert [r.getMessage() for r in records] == ["model_download_cancelled"]
    stored = service.registry.get(model_id)
    assert stored is not None and stored.status == ModelStatus.CANCELLED.value
    assert model_id not in service._tasks and model_id not in service._cancels


def test_shutdown_waits_for_workers_to_stop(
    service: ModelService,
    records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, stopped = threading.Event(), threading.Event()
    monkeypatch.setattr(downloader, "download", _blocking_worker(started, stopped))

    async def go() -> str:
        model_id = service.start_download(source_type="url", url="https://h.example/m.gguf").id
        await asyncio.to_thread(started.wait, 10)
        await service.shutdown(grace_seconds=10)
        assert stopped.is_set(), "shutdown returned while the worker was running"
        return model_id

    model_id = asyncio.run(go())
    assert _failures(records) == []
    stored = service.registry.get(model_id)
    assert stored is not None and stored.status == ModelStatus.CANCELLED.value
    assert service._tasks == {} and service._cancels == {}


def test_cancelled_task_keeps_ownership_and_blocks_late_progress(
    service: ModelService,
    records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supplementary (fake worker): the real-downloader cases are in
    ``test_download_worker_lifetime.py``."""
    started, stopped, release = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(downloader, "download", _blocking_worker(started, stopped, release))

    async def go() -> str:
        model_id = service.start_download(source_type="url", url="https://h.example/m.gguf").id
        task = service._tasks[model_id]
        try:
            await asyncio.to_thread(started.wait, 10)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=0.3)
            # The worker is still running: nothing is reported or released yet.
            assert not done, "the task finished while its worker was running"
            assert model_id in service._tasks and service._cancels[model_id].is_set()
            stored = service.registry.get(model_id)
            assert stored is not None and stored.status == ModelStatus.DOWNLOADING.value
        finally:
            release.set()  # the worker now reports progress after cancellation
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        return model_id

    model_id = asyncio.run(go())
    assert _failures(records) == []
    stored = service.registry.get(model_id)
    assert stored is not None and stored.status == ModelStatus.CANCELLED.value
    assert stored.downloaded_bytes == 0 and stored.size_bytes is None
    assert service._tasks == {} and service._cancels == {}
