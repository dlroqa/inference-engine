"""route_decision structured log + one-record-per-outcome accounting (Block 12.2a).

Exercises the shared serving helper across the normal, mid-stream-error, and
synchronous-generate()-error paths, and asserts the log is privacy-safe (no prompt
or credential content) while carrying the safe routing/latency fields.

The app resets root logging handlers at startup (``configure_logging``), which
removes pytest's ``caplog`` handler, so records are captured with a handler
attached to the ``engine.request`` logger *after* the app has started.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from engine.config import Settings
from engine.inference.base import GenerationRequest, GenerationStream
from engine.main import create_app
from tests.api.conftest import MODEL_ID
from tests.support.fake_backend import FakeBackend

LOOPBACK = ("127.0.0.1", 40000)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _capture_route_decisions() -> Iterator[list[logging.LogRecord]]:
    handler = _Capture()
    logger = logging.getLogger("engine.request")
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)


def _decisions(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.getMessage() == "route_decision"]


def _loaded(**kw: object) -> FakeBackend:
    fake = FakeBackend(tokens=["A", "B"], model_id=MODEL_ID, **kw)  # type: ignore[arg-type]
    asyncio.run(fake.load())
    return fake


def _settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", model_id=MODEL_ID)


def _chat(client: TestClient, content: str = "hi", **kw: object):
    return client.post(
        "/v1/chat/completions",
        json={"model": MODEL_ID, "messages": [{"role": "user", "content": content}]},
        **kw,
    )


def test_route_decision_logged_once_on_success(tmp_path) -> None:
    app = create_app(_settings(tmp_path), backend=_loaded())
    with TestClient(app, client=LOOPBACK) as c, _capture_route_decisions() as records:
        assert _chat(c).status_code == 200
    recs = _decisions(records)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.outcome == "ok"  # type: ignore[attr-defined]
    assert rec.backend == "primary" and rec.tier == "primary"  # type: ignore[attr-defined]
    assert rec.reason == "model_map" and rec.policy == "base"  # type: ignore[attr-defined]
    assert rec.step is None  # type: ignore[attr-defined]
    assert rec.ttft_ms is not None and rec.total_ms is not None  # type: ignore[attr-defined]
    assert rec.output_tps is not None and rec.queue_wait_ms is not None  # type: ignore[attr-defined]
    assert rec.upstream_attempts == 0  # type: ignore[attr-defined]
    assert app.state.backend_registry.entries[0].in_flight == 0  # accounted once


def test_route_decision_log_is_privacy_safe(tmp_path) -> None:
    app = create_app(_settings(tmp_path), backend=_loaded())
    prompt_marker = "SECRETPROMPT-zzz"
    key_marker = "APIKEYMARKER-zzz"
    with TestClient(app, client=LOOPBACK) as c, _capture_route_decisions() as records:
        _chat(c, content=prompt_marker, headers={"authorization": f"Bearer {key_marker}"})
    recs = _decisions(records)
    assert len(recs) == 1
    rec = recs[0]
    blob = json.dumps(dict(rec.__dict__), default=str)
    assert prompt_marker not in blob and key_marker not in blob
    assert rec.reason and rec.backend and rec.outcome == "ok"  # type: ignore[attr-defined]


def test_route_decision_logged_on_midstream_error(tmp_path) -> None:
    app = create_app(_settings(tmp_path), backend=_loaded(fail_after=1))
    with TestClient(app, client=LOOPBACK) as c, _capture_route_decisions() as records:
        resp = _chat(c)
    assert resp.status_code == 500
    recs = _decisions(records)
    assert len(recs) == 1
    assert recs[0].outcome == "error"  # type: ignore[attr-defined]
    assert recs[0].output_tps is None  # no rate sample for a failed request
    assert app.state.backend_registry.entries[0].in_flight == 0


class _RaisingBackend(FakeBackend):
    """A backend whose synchronous ``generate()`` raises before streaming starts."""

    def generate(self, request: GenerationRequest) -> GenerationStream:
        raise RuntimeError("boom in generate")


def test_route_decision_logged_on_sync_generate_error(tmp_path) -> None:
    backend = _RaisingBackend(tokens=["A"], model_id=MODEL_ID)
    asyncio.run(backend.load())
    app = create_app(_settings(tmp_path), backend=backend)
    with (
        TestClient(app, client=LOOPBACK, raise_server_exceptions=False) as c,
        _capture_route_decisions() as records,
    ):
        resp = _chat(c)
    assert resp.status_code == 500
    recs = _decisions(records)
    assert len(recs) == 1
    assert recs[0].outcome == "error" and recs[0].backend == "primary"  # type: ignore[attr-defined]
    assert app.state.backend_registry.entries[0].in_flight == 0  # accounted once
