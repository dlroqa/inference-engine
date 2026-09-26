"""Structured JSON logging formatter behavior."""

from __future__ import annotations

import json
import logging

from engine.logging_setup import JsonFormatter, configure_logging, get_logger, redact_secrets


def _record(**kwargs: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="an_event",
        args=(),
        exc_info=None,
    )


def test_formatter_emits_json_with_core_fields() -> None:
    line = JsonFormatter().format(_record())
    payload = json.loads(line)
    assert payload["event"] == "an_event"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert "ts" in payload


def test_formatter_includes_extras() -> None:
    record = _record()
    record.request_id = "abc-123"  # type: ignore[attr-defined]
    payload = json.loads(JsonFormatter().format(record))
    assert payload["request_id"] == "abc-123"


def test_formatter_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    payload = json.loads(JsonFormatter().format(record))
    assert "exc_info" in payload
    assert "ValueError" in payload["exc_info"]


def test_configure_logging_is_idempotent() -> None:
    configure_logging("DEBUG")
    configure_logging("INFO")  # second call must not stack handlers
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(get_logger("x"), logging.Logger)


def test_credentials_in_logged_urls_are_redacted() -> None:
    # uvicorn logs the WebSocket handshake path, which carries the dashboard's key.
    line = '127.0.0.1:1 - "WebSocket /ws/feed?api_key=sk-ie-SECRET&x=1" [accepted]'
    record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, line, None, None)
    out = JsonFormatter().format(record)
    assert "sk-ie-SECRET" not in out
    assert "api_key=***&x=1" in json.loads(out)["event"]
    assert redact_secrets("/ws/metrics?token=abc") == "/ws/metrics?token=***"
    assert redact_secrets("GET /logs?level=ERROR") == "GET /logs?level=ERROR"


def test_log_collector_stores_redacted_messages() -> None:
    from engine.telemetry.logbuffer import LogCollector

    collector = LogCollector(db_path=None)
    record = logging.LogRecord(
        "uvicorn.error", logging.WARNING, __file__, 1, "GET /x?api_key=sk-ie-LEAK", None, None
    )
    collector.emit(record)
    stored = str(collector.recent(limit=5))
    assert "sk-ie-LEAK" not in stored
