"""Structured JSON logging.

Emits one JSON object per log line so records are machine-parseable from the
outset. Block 0 only needs request-independent events (startup, shutdown, and
errors); per-request/trace correlation is added with the API surface in later
blocks.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from typing import Any

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        # Merge any structured extras passed via ``logger.info(msg, extra={...})``.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(level.upper())
    # Replace existing handlers so repeated calls don't duplicate output.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)


def get_logger(name: str = "engine") -> logging.Logger:
    return logging.getLogger(name)
