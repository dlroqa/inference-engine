"""Download-failure log lines never carry credentials from the failure text.

The downloader is mocked to fail deterministically; the real service failure
path runs and is awaited. Each emitted ``LogRecord`` is inspected before any
formatting (other handlers see the raw record), then formatted with the real
``JsonFormatter``. All secrets are synthetic; assertions report only which
case failed, never the payload.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from engine.config import Settings
from engine.logging_setup import JsonFormatter
from engine.models import downloader, redaction
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
    text, kept = CASES[case]
    monkeypatch.setattr(downloader, "download", _failing(downloader.DownloadError(text)))
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

    # Lifecycle is unchanged: error state, raw text kept in the registry (API
    # responses redact it), and the task is no longer tracked.
    stored = service.registry.get(model_id)
    assert stored is not None
    assert stored.status == ModelStatus.ERROR.value
    assert stored.error == text
    assert model_id not in service._tasks and model_id not in service._cancels


@pytest.mark.parametrize("text", USEFUL)
def test_non_sensitive_failures_stay_useful(
    service: ModelService,
    records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    exc = (
        downloader.ChecksumMismatch(text)
        if text.startswith("checksum")
        else downloader.DownloadError(text)
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

    monkeypatch.setattr(downloader, "download", _failing(Unprintable()))
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
