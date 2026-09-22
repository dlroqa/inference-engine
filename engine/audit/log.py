"""Append-only, hash-chained audit log for operator/security actions.

Each event records who did what to which object, plus a hash chain:

    hash = sha256(prev_hash + canonical_json(ts, actor, action, target, detail))

Because every row commits to the previous row's hash, any later insertion,
deletion, or edit of a past row breaks :meth:`AuditLog.verify`. Details carry only
structured, non-secret metadata — never prompts, responses, tokens, or key
secrets.

This is tamper-*evident*, not tamper-*proof*: an attacker with write access to the
database could recompute the whole chain. For stronger guarantees, ship the log
off-box (a later, ops-time concern); the chain still detects accidental or partial
corruption and after-the-fact edits.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from engine.store.db import connect, connect_readonly

GENESIS_HASH = "0" * 64


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat()


def _canonical(ts: str, actor: str, action: str, target: str | None, detail: dict) -> str:
    # Sorted, separator-stable JSON so the hash is reproducible across processes.
    payload = {
        "ts": ts,
        "actor": actor,
        "action": action,
        "target": target,
        "detail": detail,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _row_hash(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class AuditEvent:
    id: int
    ts: str
    actor: str
    action: str
    target: str | None
    detail: dict
    prev_hash: str
    hash: str


@dataclass(slots=True)
class VerifyResult:
    ok: bool
    count: int
    first_bad_id: int | None = None


class AuditLog:
    """SQLite-backed hash-chained audit log (short-lived connections)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _last_hash(self, conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1;").fetchone()
        return row["hash"] if row is not None else GENESIS_HASH

    def record(
        self,
        action: str,
        *,
        actor: str,
        target: str | None = None,
        detail: dict | None = None,
    ) -> AuditEvent:
        """Append one event to the chain and return it."""
        ts = _now_iso()
        detail = detail or {}
        conn = connect(self._db_path)
        try:
            prev_hash = self._last_hash(conn)
            canonical = _canonical(ts, actor, action, target, detail)
            row_hash = _row_hash(prev_hash, canonical)
            cur = conn.execute(
                "INSERT INTO audit_events (ts, actor, action, target, detail, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?);",
                (
                    ts,
                    actor,
                    action,
                    target,
                    json.dumps(detail, sort_keys=True),
                    prev_hash,
                    row_hash,
                ),
            )
            conn.commit()
            event_id = int(cur.lastrowid or 0)
        finally:
            conn.close()
        return AuditEvent(
            id=event_id,
            ts=ts,
            actor=actor,
            action=action,
            target=target,
            detail=detail,
            prev_hash=prev_hash,
            hash=row_hash,
        )

    def list(self, *, limit: int = 100) -> list[AuditEvent]:
        conn = connect_readonly(self._db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?;", (limit,)
            ).fetchall()
        finally:
            conn.close()
        return [
            AuditEvent(
                id=r["id"],
                ts=r["ts"],
                actor=r["actor"],
                action=r["action"],
                target=r["target"],
                detail=json.loads(r["detail"]),
                prev_hash=r["prev_hash"],
                hash=r["hash"],
            )
            for r in rows
        ]

    def verify(self) -> VerifyResult:
        """Recompute the chain in order; report the first row that fails."""
        conn = connect_readonly(self._db_path)
        try:
            rows = conn.execute("SELECT * FROM audit_events ORDER BY id ASC;").fetchall()
        finally:
            conn.close()
        prev = GENESIS_HASH
        count = 0
        for r in rows:
            count += 1
            canonical = _canonical(
                r["ts"], r["actor"], r["action"], r["target"], json.loads(r["detail"])
            )
            expected = _row_hash(prev, canonical)
            if r["prev_hash"] != prev or r["hash"] != expected:
                return VerifyResult(ok=False, count=count, first_bad_id=int(r["id"]))
            prev = r["hash"]
        return VerifyResult(ok=True, count=count)
