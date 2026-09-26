"""API-key store: create (display-once), verify, revoke, list.

Tokens are high-entropy random strings; only their SHA-256 hash is stored, so a
database leak never exposes usable credentials. The plaintext token is returned
exactly once, at creation.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from engine.store.db import connect

KEY_PREFIX = "sk-ie-"
ROLE_OPERATOR = "operator"
ROLE_CLIENT = "client"
_PREFIX_DISPLAY_LEN = 12  # e.g. "sk-ie-ab12" — non-secret, for display/lookup


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class KeyRecord:
    id: str
    prefix: str
    label: str | None
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    #: Owning billing client (Block 11), or ``None`` for an operator key.
    client_id: str | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def role(self) -> str:
        """``"client"`` for a key owned by a billing client, else ``"operator"``.

        Derived from ``api_keys.client_id`` (migration 0006): client-owned keys may
        call inference and ``/client/*`` but never operator surfaces.
        """
        return ROLE_CLIENT if self.client_id is not None else ROLE_OPERATOR


def _row_to_record(row: sqlite3.Row) -> KeyRecord:
    return KeyRecord(
        id=row["id"],
        prefix=row["prefix"],
        label=row["label"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
        client_id=row["client_id"] if "client_id" in row.keys() else None,
    )


class KeyStore:
    """SQLite-backed API-key store (short-lived connections per operation)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def create(self, label: str | None = None) -> tuple[KeyRecord, str]:
        """Create a key. Returns its record and the plaintext token (shown once)."""
        token = KEY_PREFIX + secrets.token_urlsafe(32)
        key_id = uuid.uuid4().hex
        prefix = token[:_PREFIX_DISPLAY_LEN]
        created_at = _now_iso()
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO api_keys (id, key_hash, prefix, label, created_at) "
                    "VALUES (?, ?, ?, ?, ?);",
                    (key_id, hash_token(token), prefix, label, created_at),
                )
        finally:
            conn.close()
        record = KeyRecord(
            id=key_id,
            prefix=prefix,
            label=label,
            created_at=created_at,
            last_used_at=None,
            revoked_at=None,
        )
        return record, token

    def verify(self, token: str) -> KeyRecord | None:
        """Return the record for a valid, non-revoked token, else ``None``.

        Updates ``last_used_at`` on success.
        """
        if not token:
            return None
        conn = connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash = ?;", (hash_token(token),)
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                return None
            with conn:
                conn.execute(
                    "UPDATE api_keys SET last_used_at = ? WHERE id = ?;",
                    (_now_iso(), row["id"]),
                )
            return _row_to_record(row)
        finally:
            conn.close()

    def revoke(self, key_id: str) -> bool:
        """Revoke a key. Returns True if a live key was revoked."""
        conn = connect(self._db_path)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL;",
                    (_now_iso(), key_id),
                )
            return cur.rowcount > 0
        finally:
            conn.close()

    def delete(self, key_id: str) -> bool:
        """Permanently remove a key row. Returns True if a row was deleted.

        Attribution rows in ``usage_events``/``log_events`` keep the key id as a
        plain string, so historical accounting is unaffected by the removal.
        """
        conn = connect(self._db_path)
        try:
            with conn:
                cur = conn.execute("DELETE FROM api_keys WHERE id = ?;", (key_id,))
            return cur.rowcount > 0
        finally:
            conn.close()

    def get(self, key_id: str) -> KeyRecord | None:
        conn = connect(self._db_path)
        try:
            row = conn.execute("SELECT * FROM api_keys WHERE id = ?;", (key_id,)).fetchone()
            return _row_to_record(row) if row is not None else None
        finally:
            conn.close()

    def list(self) -> list[KeyRecord]:
        conn = connect(self._db_path)
        try:
            rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at;").fetchall()
            return [_row_to_record(r) for r in rows]
        finally:
            conn.close()
