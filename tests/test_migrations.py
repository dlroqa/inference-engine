"""Migration applies to a new SQLite database (Block 0 required test)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from engine.store.db import connect
from engine.store.migrations import (
    applied_versions,
    apply_migrations,
    discover_migrations,
    migrations_at_head,
)


def _table_exists(conn: object, name: str) -> bool:
    row = conn.execute(  # type: ignore[attr-defined]
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (name,)
    ).fetchone()
    return row is not None


def test_apply_to_fresh_database(tmp_path: Path) -> None:
    conn = connect(tmp_path / "fresh.db")
    try:
        applied = apply_migrations(conn)
        assert "0001" in applied
        assert _table_exists(conn, "settings")
        assert _table_exists(conn, "schema_migrations")
        assert migrations_at_head(conn)
    finally:
        conn.close()


def test_apply_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "idem.db")
    try:
        first = apply_migrations(conn)
        second = apply_migrations(conn)
        assert first  # something applied the first time
        assert second == []  # nothing applied the second time
        assert migrations_at_head(conn)
    finally:
        conn.close()


def test_recorded_versions_match_discovered(tmp_path: Path) -> None:
    conn = connect(tmp_path / "match.db")
    try:
        apply_migrations(conn)
        discovered = {v for v, _ in discover_migrations()}
        assert applied_versions(conn) == discovered
    finally:
        conn.close()


def test_wal_mode_enabled(tmp_path: Path) -> None:
    conn = connect(tmp_path / "wal.db")
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_settings_table_usable(tmp_path: Path) -> None:
    conn = connect(tmp_path / "use.db")
    try:
        apply_migrations(conn)
        with conn:
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?);", ("k", "v"))
        row = conn.execute("SELECT value FROM settings WHERE key=?;", ("k",)).fetchone()
        assert row[0] == "v"
    finally:
        conn.close()


def test_failed_migration_is_atomic(tmp_path: Path) -> None:
    """A migration that fails partway must leave nothing applied or recorded."""
    migs = tmp_path / "migs"
    migs.mkdir()
    # First statement succeeds; second is invalid SQL -> whole migration must roll back.
    (migs / "0001_broken.sql").write_text(
        "CREATE TABLE good (id INTEGER);\nCREATE TABLE bad (;\n", encoding="utf-8"
    )
    conn = connect(tmp_path / "atomic.db")
    try:
        with pytest.raises(sqlite3.Error):
            apply_migrations(conn, migs)
        # Neither the table from the first statement nor the bookkeeping row persist.
        assert not _table_exists(conn, "good")
        assert applied_versions(conn) == set()
        assert not migrations_at_head(conn, migs)
    finally:
        conn.close()


def test_migration_with_transaction_control_rejected(tmp_path: Path) -> None:
    migs = tmp_path / "migs"
    migs.mkdir()
    (migs / "0001_txn.sql").write_text(
        "BEGIN;\nCREATE TABLE t (id INTEGER);\nCOMMIT;\n", encoding="utf-8"
    )
    conn = connect(tmp_path / "txn.db")
    try:
        with pytest.raises(ValueError):
            apply_migrations(conn, migs)
    finally:
        conn.close()


def test_misnamed_migration_rejected(tmp_path: Path) -> None:
    bad_dir = tmp_path / "migs"
    bad_dir.mkdir()
    (bad_dir / "not_numbered.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError):
        discover_migrations(bad_dir)
