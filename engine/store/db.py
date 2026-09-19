"""SQLite connection management.

SQLite in WAL mode is the zero-configuration default persistence store. Postgres
and a scale-out story are deferred to later blocks (see the architecture plan
§6); the schema and access here are deliberately minimal.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a read/write SQLite connection with safe, boring defaults.

    Ensures the parent directory exists, enables WAL journaling for better
    concurrent read behavior, enforces foreign keys, and returns rows as
    :class:`sqlite3.Row` for name-based access.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a lightweight read-only connection.

    Used by frequently-polled probes (e.g. readiness) to avoid the directory
    creation and write-oriented PRAGMAs of :func:`connect`. Raises if the
    database file does not exist, which callers treat as "not reachable".
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
