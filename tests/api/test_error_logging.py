"""A forced backend error yields a structured, request-correlated log record.

This is the Block 4 exit-criterion evidence: an operator can tell *whose* failure
it was (category) and *where* it happened (stage + stacktrace), tied to the
request id — without any prompt/response text being stored.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app
from engine.telemetry.logbuffer import read_log_events
from tests.api.conftest import MODEL_ID
from tests.support.fake_backend import FakeBackend


def test_backend_failure_writes_correlated_log(tmp_settings: Settings) -> None:
    fake = FakeBackend(tokens=["a", "b"], fail_after=0, model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(tmp_settings, backend=fake)
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        resp = c.post(
            "/v1/chat/completions",
            json={"model": MODEL_ID, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 500
        request_id = resp.headers["x-request-id"]

        rows = read_log_events(tmp_settings.db_path, limit=50)

    row = next(r for r in rows if r["request_id"] == request_id)
    assert row["category"] == "backend"
    assert row["stage"] == "generation"
    assert row["stacktrace"] is not None  # correlated failure carries a stacktrace
    assert row["route"] == "/v1/chat/completions"
    # No prompt text is stored anywhere in the record.
    assert "hi" not in (row["detail"] or "")


def test_streaming_failure_emits_error_frame_and_logs(tmp_settings: Settings) -> None:
    # Fail after the first token so the error happens mid-stream (headers sent).
    fake = FakeBackend(tokens=["a", "b", "c"], fail_after=1, model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(tmp_settings, backend=fake)
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        with c.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())
        rows = read_log_events(tmp_settings.db_path, limit=50)

    # A terminal error frame and stream terminator are present.
    assert '"error"' in body
    assert "data: [DONE]" in body
    # The mid-stream failure was logged as a backend/generation error.
    assert any(r["category"] == "backend" and r["stage"] == "generation" for r in rows)


def test_client_error_classified_not_as_backend(tmp_settings: Settings) -> None:
    fake = FakeBackend(tokens=["a"], model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(tmp_settings, backend=fake)
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        # Wrong model id -> a client/model error, never a backend failure.
        resp = c.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 404
        rows = read_log_events(tmp_settings.db_path, limit=50)
    assert any(r["category"] == "model" for r in rows)
    assert all(r["category"] != "backend" for r in rows)
