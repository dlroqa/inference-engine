"""Per-client event stream backing client SSE (Block 11.5).

Three pieces:

- :class:`ClientEventLog` — a durable, ordered, per-client append-only log. Its
  autoincrement id is the SSE cursor (``Last-Event-ID``); ``UNIQUE(client_id,
  event_id)`` makes emission idempotent.
- :class:`ClientEventNotifier` — an in-process wakeup so a live SSE stream pushes
  without polling the database. Single-process by design (multi-node fan-out is a
  later, scale-time concern, exactly like the operator event bus).
- :class:`ClientEventEmitter` — the single fan-out point used by the billing
  lifecycle and usage-threshold sites: it records to the durable log (for SSE) and
  hands the event to the webhook dispatcher (for outbound webhooks). Each channel
  honors its own enabled flag, so either or both can be off.

The emitter never raises into its caller — an event-delivery problem must not
break the request or lifecycle action that produced the event.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from engine.logging_setup import get_logger
from engine.store.db import connect

_log = get_logger("engine.client_events")


@dataclass(slots=True)
class ClientEvent:
    id: int
    event_id: str
    type: str
    payload: str  # JSON envelope
    ts: float


def _envelope(event_id: str, event_type: str, data: dict[str, Any]) -> str:
    envelope = {
        "id": event_id,
        "type": event_type,
        "timestamp": _dt.datetime.now(tz=_dt.UTC).isoformat(),
        "data": data,
    }
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True)


class ClientEventLog:
    """SQLite-backed per-client event log (short-lived connections per operation)."""

    def __init__(
        self,
        db_path: Path,
        *,
        retention_max_age_s: float = 0.0,
        retention_max_per_client: int = 0,
    ) -> None:
        self._db_path = db_path
        self._max_age_s = retention_max_age_s
        self._max_per_client = retention_max_per_client

    def append(
        self,
        *,
        client_id: str,
        event_id: str,
        event_type: str,
        payload: str,
        now: float | None = None,
    ) -> int | None:
        """Append an event. Returns the new row id, or ``None`` if it already
        existed (idempotent per ``(client_id, event_id)``)."""
        now = _dt.datetime.now(tz=_dt.UTC).timestamp() if now is None else now
        conn = connect(self._db_path)
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO client_events "
                    "(client_id, event_id, type, payload, ts) VALUES (?, ?, ?, ?, ?);",
                    (client_id, event_id, event_type, payload, now),
                )
                if cur.rowcount == 0:
                    return None
                new_id = int(cur.lastrowid)  # type: ignore[arg-type]
                self._prune(conn, client_id, now)
            return new_id
        finally:
            conn.close()

    def _prune(self, conn: object, client_id: str, now: float) -> None:
        if self._max_age_s > 0:
            conn.execute(  # type: ignore[attr-defined]
                "DELETE FROM client_events WHERE client_id = ? AND ts < ?;",
                (client_id, now - self._max_age_s),
            )
        if self._max_per_client > 0:
            conn.execute(  # type: ignore[attr-defined]
                "DELETE FROM client_events WHERE client_id = ? AND id NOT IN "
                "(SELECT id FROM client_events WHERE client_id = ? "
                " ORDER BY id DESC LIMIT ?);",
                (client_id, client_id, self._max_per_client),
            )

    def list_since(self, client_id: str, since_id: int, limit: int = 100) -> list[ClientEvent]:
        """Events for a client with ``id > since_id``, oldest first."""
        conn = connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT id, event_id, type, payload, ts FROM client_events "
                "WHERE client_id = ? AND id > ? ORDER BY id LIMIT ?;",
                (client_id, since_id, limit),
            ).fetchall()
            return [
                ClientEvent(
                    id=r["id"],
                    event_id=r["event_id"],
                    type=r["type"],
                    payload=r["payload"],
                    ts=r["ts"],
                )
                for r in rows
            ]
        finally:
            conn.close()

    def latest_id(self, client_id: str) -> int:
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM client_events WHERE client_id = ?;",
                (client_id,),
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()


class ClientEventNotifier:
    """In-process per-client wakeup for live SSE (no cross-process fan-out)."""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def event(self, client_id: str) -> asyncio.Event:
        """The current wakeup for a client. A subscriber captures this *before*
        reading the log, then awaits it, so a notify during the read is not lost."""
        return self._events.setdefault(client_id, asyncio.Event())

    def notify(self, client_id: str) -> None:
        existing = self._events.get(client_id)
        if existing is not None:
            existing.set()
        # Rotate to a fresh, unset event so the next subscriber blocks again.
        self._events[client_id] = asyncio.Event()


class _Dispatcher(Protocol):
    def emit(
        self,
        *,
        client_id: str,
        event_type: str,
        data: dict[str, Any],
        event_id: str | None = ...,
        now: float | None = ...,
    ) -> int: ...


class ClientEventEmitter:
    """Single fan-out point: durable client log (SSE) + webhook dispatcher (push)."""

    def __init__(
        self,
        *,
        dispatcher: _Dispatcher | None = None,
        event_log: ClientEventLog | None = None,
        notifier: ClientEventNotifier | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._log = event_log
        self._notifier = notifier

    def emit(
        self,
        *,
        client_id: str,
        event_type: str,
        data: dict[str, Any],
        event_id: str | None = None,
        now: float | None = None,
    ) -> None:
        eid = event_id or f"evt_{uuid.uuid4().hex}"
        # Outbound webhooks (respects its own enabled flag / endpoint set).
        if self._dispatcher is not None:
            try:
                self._dispatcher.emit(
                    client_id=client_id,
                    event_type=event_type,
                    data=data,
                    event_id=eid,
                    now=now,
                )
            except Exception:
                _log.exception("client_event_webhook_emit_failed", extra={"type": event_type})
        # Durable per-client log for SSE.
        if self._log is not None:
            try:
                new_id = self._log.append(
                    client_id=client_id,
                    event_id=eid,
                    event_type=event_type,
                    payload=_envelope(eid, event_type, data),
                    now=now,
                )
                if new_id is not None and self._notifier is not None:
                    self._notifier.notify(client_id)
            except Exception:
                _log.exception("client_event_log_append_failed", extra={"type": event_type})
