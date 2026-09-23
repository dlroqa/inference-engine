"""Persistence for outbound webhooks: endpoints, rotatable secrets, and the
delivery log/queue (one table serving both roles).

All connections are short-lived per operation, matching the rest of the engine's
SQLite access. The delivery table's ``UNIQUE(endpoint_id, event_id)`` makes
fan-out and re-emission idempotent: enqueuing the same event to the same endpoint
twice inserts one row.
"""

from __future__ import annotations

import datetime as _dt
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from engine.billing.webhooks import signing
from engine.store.db import connect

STATUS_PENDING = "pending"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat()


@dataclass(slots=True)
class Endpoint:
    id: str
    client_id: str
    url: str
    description: str | None
    disabled: bool
    event_types: tuple[str, ...] | None  # None == all events


@dataclass(slots=True)
class Delivery:
    id: str
    endpoint_id: str
    event_id: str
    event_type: str
    payload: str
    status: str
    attempts: int
    next_attempt_at: float
    last_status_code: int | None
    last_error: str | None
    created_at: float
    updated_at: float


def _endpoint_from_row(row: object) -> Endpoint:
    types_raw = row["event_types"]  # type: ignore[index]
    return Endpoint(
        id=row["id"],  # type: ignore[index]
        client_id=row["client_id"],  # type: ignore[index]
        url=row["url"],  # type: ignore[index]
        description=row["description"],  # type: ignore[index]
        disabled=bool(row["disabled"]),  # type: ignore[index]
        event_types=tuple(json.loads(types_raw)) if types_raw else None,
    )


def _delivery_from_row(row: object) -> Delivery:
    return Delivery(
        id=row["id"],  # type: ignore[index]
        endpoint_id=row["endpoint_id"],  # type: ignore[index]
        event_id=row["event_id"],  # type: ignore[index]
        event_type=row["event_type"],  # type: ignore[index]
        payload=row["payload"],  # type: ignore[index]
        status=row["status"],  # type: ignore[index]
        attempts=row["attempts"],  # type: ignore[index]
        next_attempt_at=row["next_attempt_at"],  # type: ignore[index]
        last_status_code=row["last_status_code"],  # type: ignore[index]
        last_error=row["last_error"],  # type: ignore[index]
        created_at=row["created_at"],  # type: ignore[index]
        updated_at=row["updated_at"],  # type: ignore[index]
    )


class WebhookStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    # ---- endpoints -------------------------------------------------------

    def create_endpoint(
        self,
        *,
        client_id: str,
        url: str,
        description: str | None = None,
        event_types: list[str] | None = None,
    ) -> tuple[Endpoint, str]:
        """Create an endpoint with one signing secret. Returns the endpoint and the
        plaintext secret (shown once, like an API key)."""
        endpoint_id = uuid.uuid4().hex
        secret = signing.generate_secret()
        types_json = json.dumps(list(event_types)) if event_types is not None else None
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO webhook_endpoints "
                    "(id, client_id, url, description, disabled, event_types, created_at) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?);",
                    (endpoint_id, client_id, url, description, types_json, _now_iso()),
                )
                conn.execute(
                    "INSERT INTO webhook_secrets (id, endpoint_id, secret, created_at) "
                    "VALUES (?, ?, ?, ?);",
                    (uuid.uuid4().hex, endpoint_id, secret, _now_iso()),
                )
        finally:
            conn.close()
        endpoint = Endpoint(
            id=endpoint_id,
            client_id=client_id,
            url=url,
            description=description,
            disabled=False,
            event_types=tuple(event_types) if event_types is not None else None,
        )
        return endpoint, secret

    def get_endpoint(self, endpoint_id: str) -> Endpoint | None:
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT * FROM webhook_endpoints WHERE id = ?;", (endpoint_id,)
            ).fetchone()
            return _endpoint_from_row(row) if row is not None else None
        finally:
            conn.close()

    def list_endpoints(self, client_id: str | None = None) -> list[Endpoint]:
        conn = connect(self._db_path)
        try:
            if client_id is None:
                rows = conn.execute(
                    "SELECT * FROM webhook_endpoints ORDER BY created_at;"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM webhook_endpoints WHERE client_id = ? ORDER BY created_at;",
                    (client_id,),
                ).fetchall()
            return [_endpoint_from_row(r) for r in rows]
        finally:
            conn.close()

    def set_disabled(self, endpoint_id: str, disabled: bool) -> bool:
        conn = connect(self._db_path)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE webhook_endpoints SET disabled = ? WHERE id = ?;",
                    (1 if disabled else 0, endpoint_id),
                )
            return cur.rowcount > 0
        finally:
            conn.close()

    def delete_endpoint(self, endpoint_id: str) -> bool:
        conn = connect(self._db_path)
        try:
            with conn:
                cur = conn.execute("DELETE FROM webhook_endpoints WHERE id = ?;", (endpoint_id,))
            return cur.rowcount > 0
        finally:
            conn.close()

    # ---- secrets / rotation ---------------------------------------------

    def active_secrets(self, endpoint_id: str, now: float | None = None) -> list[str]:
        """Return the endpoint's active (non-expired) secrets, newest first."""
        now = time.time() if now is None else now
        conn = connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT secret FROM webhook_secrets "
                "WHERE endpoint_id = ? AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY created_at DESC;",
                (endpoint_id, now),
            ).fetchall()
            return [r["secret"] for r in rows]
        finally:
            conn.close()

    def rotate_secret(self, endpoint_id: str, *, grace_s: float, now: float | None = None) -> str:
        """Add a new signing secret and expire the current one after ``grace_s``.

        Returns the new plaintext secret. During the grace window the endpoint has
        two active secrets, so deliveries are signed with both and receivers using
        either verify successfully.
        """
        now = time.time() if now is None else now
        new_secret = signing.generate_secret()
        conn = connect(self._db_path)
        try:
            with conn:
                # Expire any currently-unexpired secrets at now + grace.
                conn.execute(
                    "UPDATE webhook_secrets SET expires_at = ? "
                    "WHERE endpoint_id = ? AND expires_at IS NULL;",
                    (now + grace_s, endpoint_id),
                )
                conn.execute(
                    "INSERT INTO webhook_secrets (id, endpoint_id, secret, created_at) "
                    "VALUES (?, ?, ?, ?);",
                    (uuid.uuid4().hex, endpoint_id, new_secret, _now_iso()),
                )
        finally:
            conn.close()
        return new_secret

    # ---- enqueue / fan-out ----------------------------------------------

    def enqueue_event(
        self,
        *,
        client_id: str,
        event_id: str,
        event_type: str,
        payload: str,
        now: float | None = None,
    ) -> int:
        """Fan one event out to a client's matching, enabled endpoints.

        Returns the number of delivery rows created. Idempotent per
        (endpoint, event_id): a repeated event enqueues no duplicate rows.
        """
        now = time.time() if now is None else now
        created = 0
        conn = connect(self._db_path)
        try:
            endpoints = conn.execute(
                "SELECT * FROM webhook_endpoints WHERE client_id = ? AND disabled = 0;",
                (client_id,),
            ).fetchall()
            with conn:
                for row in endpoints:
                    endpoint = _endpoint_from_row(row)
                    if endpoint.event_types is not None and event_type not in endpoint.event_types:
                        continue
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO webhook_deliveries "
                        "(id, endpoint_id, event_id, event_type, payload, status, attempts, "
                        " next_attempt_at, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?);",
                        (
                            uuid.uuid4().hex,
                            endpoint.id,
                            event_id,
                            event_type,
                            payload,
                            now,
                            now,
                            now,
                        ),
                    )
                    created += cur.rowcount
        finally:
            conn.close()
        return created

    # ---- delivery queue --------------------------------------------------

    def claim_due(self, now: float | None = None, limit: int = 50) -> list[Delivery]:
        now = time.time() if now is None else now
        conn = connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM webhook_deliveries "
                "WHERE status = 'pending' AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at LIMIT ?;",
                (now, limit),
            ).fetchall()
            return [_delivery_from_row(r) for r in rows]
        finally:
            conn.close()

    def mark_succeeded(self, delivery_id: str, status_code: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._update(
            delivery_id,
            status=STATUS_SUCCEEDED,
            attempts_inc=True,
            status_code=status_code,
            error=None,
            next_attempt_at=None,
            now=now,
        )

    def mark_retry(
        self,
        delivery_id: str,
        *,
        next_attempt_at: float,
        status_code: int | None,
        error: str | None,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        self._update(
            delivery_id,
            status=STATUS_PENDING,
            attempts_inc=True,
            status_code=status_code,
            error=error,
            next_attempt_at=next_attempt_at,
            now=now,
        )

    def mark_dead(
        self,
        delivery_id: str,
        *,
        status_code: int | None,
        error: str | None,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        self._update(
            delivery_id,
            status=STATUS_DEAD,
            attempts_inc=True,
            status_code=status_code,
            error=error,
            next_attempt_at=None,
            now=now,
        )

    def _update(
        self,
        delivery_id: str,
        *,
        status: str,
        attempts_inc: bool,
        status_code: int | None,
        error: str | None,
        next_attempt_at: float | None,
        now: float,
    ) -> None:
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE webhook_deliveries SET "
                    " status = ?, "
                    " attempts = attempts + ?, "
                    " last_status_code = ?, "
                    " last_error = ?, "
                    " next_attempt_at = COALESCE(?, next_attempt_at), "
                    " updated_at = ? "
                    "WHERE id = ?;",
                    (
                        status,
                        1 if attempts_inc else 0,
                        status_code,
                        error,
                        next_attempt_at,
                        now,
                        delivery_id,
                    ),
                )
        finally:
            conn.close()

    # ---- log / replay ----------------------------------------------------

    def get_delivery(self, delivery_id: str) -> Delivery | None:
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT * FROM webhook_deliveries WHERE id = ?;", (delivery_id,)
            ).fetchone()
            return _delivery_from_row(row) if row is not None else None
        finally:
            conn.close()

    def list_deliveries(
        self,
        *,
        endpoint_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Delivery]:
        clauses: list[str] = []
        params: list[object] = []
        if endpoint_id is not None:
            clauses.append("endpoint_id = ?")
            params.append(endpoint_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        conn = connect(self._db_path)
        try:
            rows = conn.execute(
                f"SELECT * FROM webhook_deliveries{where} ORDER BY created_at DESC LIMIT ?;",
                params,
            ).fetchall()
            return [_delivery_from_row(r) for r in rows]
        finally:
            conn.close()

    def replay(self, delivery_id: str, now: float | None = None) -> bool:
        """Re-enqueue a delivery: reset it to pending, due now, attempts cleared."""
        now = time.time() if now is None else now
        conn = connect(self._db_path)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE webhook_deliveries SET "
                    " status = 'pending', attempts = 0, next_attempt_at = ?, "
                    " last_error = NULL, updated_at = ? "
                    "WHERE id = ?;",
                    (now, now, delivery_id),
                )
            return cur.rowcount > 0
        finally:
            conn.close()
