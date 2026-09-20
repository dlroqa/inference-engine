"""A bounded mirror of recent structured log events.

Installs a :class:`logging.Handler` that captures structured log records into:

1. an in-memory ring buffer (for a fast Logs tail and the diagnostics bundle), and
2. a bounded ``log_events`` SQLite table (durable across a restart, trimmed to a
   row cap) for records at or above a configurable level (default ``WARNING``).

Only structured metadata is stored — level, request id, route, model, key id,
error category, stage, a redacted message, and a stacktrace on failures. Raw
prompts, responses, and secrets are never logged by the engine and never captured
here. The handler is defensive: a logging call must never raise because the mirror
had a problem (e.g. the database is momentarily locked).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from engine.store.db import connect

# Extra fields we lift from ``logger.info(..., extra={...})`` into columns.
_STRUCTURED_FIELDS = ("request_id", "route", "model", "key_id", "category", "stage", "detail")


class LogCollector(logging.Handler):
    """Captures structured logs into a ring buffer and the ``log_events`` table."""

    def __init__(
        self,
        db_path: Path | None,
        *,
        ring_size: int = 500,
        db_level: int = logging.WARNING,
        max_rows: int = 2000,
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self._db_path = db_path
        self._ring: deque[dict[str, Any]] = deque(maxlen=ring_size)
        self._db_level = db_level
        self._max_rows = max_rows
        self._lock = threading.Lock()
        self._since_trim = 0

    def _record_dict(self, record: logging.LogRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for field in _STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["stacktrace"] = self.format_stack(record)
        return payload

    def format_stack(self, record: logging.LogRecord) -> str | None:
        if not record.exc_info:
            return None
        formatter = logging.Formatter()
        return formatter.formatException(record.exc_info)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = self._record_dict(record)
            with self._lock:
                self._ring.append(payload)
            if record.levelno >= self._db_level and self._db_path is not None:
                self._write_row(payload)
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)

    def _write_row(self, payload: dict[str, Any]) -> None:
        try:
            conn = connect(self._db_path)  # type: ignore[arg-type]
        except Exception:  # pragma: no cover - db momentarily unavailable
            return
        try:
            with conn:
                conn.execute(
                    "INSERT INTO log_events "
                    "(ts, level, logger, event, request_id, route, model, key_id, "
                    " category, stage, detail, stacktrace) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    (
                        payload.get("ts", time.time()),
                        payload.get("level"),
                        payload.get("logger"),
                        payload.get("event"),
                        payload.get("request_id"),
                        payload.get("route"),
                        payload.get("model"),
                        payload.get("key_id"),
                        payload.get("category"),
                        payload.get("stage"),
                        payload.get("detail"),
                        payload.get("stacktrace"),
                    ),
                )
                self._since_trim += 1
                if self._since_trim >= 50:
                    self._since_trim = 0
                    conn.execute(
                        "DELETE FROM log_events WHERE id NOT IN "
                        "(SELECT id FROM log_events ORDER BY id DESC LIMIT ?);",
                        (self._max_rows,),
                    )
        except Exception:  # pragma: no cover - db momentarily locked
            pass
        finally:
            conn.close()

    def recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Recent structured log records, oldest first."""
        with self._lock:
            items = list(self._ring)
        if limit is not None and limit >= 0:
            items = items[-limit:]
        return items


def read_log_events(
    db_path: Path,
    *,
    limit: int = 200,
    level: str | None = None,
    request_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read recent rows from the ``log_events`` table (newest first)."""
    clauses: list[str] = []
    params: list[Any] = []
    if level:
        clauses.append("level = ?")
        params.append(level.upper())
    if request_id:
        clauses.append("request_id = ?")
        params.append(request_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, limit))
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT ts, level, logger, event, request_id, route, model, key_id, "
            f"category, stage, detail, stacktrace FROM log_events {where} "
            f"ORDER BY id DESC LIMIT ?;",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
