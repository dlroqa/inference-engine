"""Tamper-evident audit log (Block 9b)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from engine.audit import GENESIS_HASH, AuditLog
from engine.config import Settings
from engine.store.db import connect
from engine.store.migrations import apply_migrations


def _audit(tmp_path: Path) -> AuditLog:
    settings = Settings(data_dir=tmp_path)
    conn = connect(settings.db_path)  # type: ignore[arg-type]
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return AuditLog(settings.db_path)  # type: ignore[arg-type]


def test_records_form_a_valid_chain(tmp_path: Path) -> None:
    log = _audit(tmp_path)
    e1 = log.record("key.create", actor="local", target="k1", detail={"label": "a"})
    e2 = log.record("model.load", actor="k1", target="m1")
    assert e1.prev_hash == GENESIS_HASH
    assert e2.prev_hash == e1.hash
    result = log.verify()
    assert result.ok is True
    assert result.count == 2


def test_list_is_newest_first_and_secret_free(tmp_path: Path) -> None:
    log = _audit(tmp_path)
    log.record("key.create", actor="local", target="k1")
    log.record("key.revoke", actor="local", target="k1")
    events = log.list()
    assert [e.action for e in events] == ["key.revoke", "key.create"]


def test_tamper_is_detected(tmp_path: Path) -> None:
    log = _audit(tmp_path)
    log.record("key.create", actor="local", target="k1")
    log.record("key.revoke", actor="local", target="k1")
    settings = Settings(data_dir=tmp_path)

    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.execute("UPDATE audit_events SET target = 'HACKED' WHERE id = 1;")
        conn.commit()
    finally:
        conn.close()

    result = log.verify()
    assert result.ok is False
    assert result.first_bad_id == 1


def test_deleting_a_row_breaks_the_chain(tmp_path: Path) -> None:
    log = _audit(tmp_path)
    log.record("a", actor="local")
    log.record("b", actor="local")
    log.record("c", actor="local")
    settings = Settings(data_dir=tmp_path)
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.execute("DELETE FROM audit_events WHERE id = 2;")
        conn.commit()
    finally:
        conn.close()
    # Row 3's prev_hash no longer matches row 1's hash.
    result = log.verify()
    assert result.ok is False
    assert result.first_bad_id == 3


def test_empty_log_verifies(tmp_path: Path) -> None:
    log = _audit(tmp_path)
    result = log.verify()
    assert result.ok is True
    assert result.count == 0
