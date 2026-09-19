"""Structured JSON logging formatter behavior."""

from __future__ import annotations

import json
import logging

from engine.logging_setup import JsonFormatter, configure_logging, get_logger


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
