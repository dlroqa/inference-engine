"""Minimal, transparent SQL migration runner.

Applies ordered ``NNNN_name.sql`` files from the ``migrations/`` directory once
each, recording applied versions in a ``schema_migrations`` table. This is the
Block 0 "migrations from the outset" mechanism; a heavier Alembic/Postgres path
is planned for later blocks (architecture plan §6). Running migrations is
idempotent: already-applied files are skipped.
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME_RE = re.compile(r"^(\d{4})_.+\.sql$")


def _ensure_tracking_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        """
    )


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    """Return ``(version, path)`` pairs sorted by version.

    Raises :class:`ValueError` if a ``.sql`` file does not match the required
    ``NNNN_name.sql`` naming convention, so misnamed migrations fail loudly.
    """
    found: list[tuple[str, Path]] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise ValueError(f"Migration file {path.name!r} must match NNNN_name.sql")
        found.append((match.group(1), path))
    return found


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    _ensure_tracking_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations;").fetchall()
    return {row[0] for row in rows}


def apply_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[str]:
    """Apply all pending migrations. Returns the versions newly applied."""
    _ensure_tracking_table(conn)
    already = applied_versions(conn)
    newly_applied: list[str] = []

    for version, path in discover_migrations(migrations_dir):
        if version in already:
            continue
        sql = path.read_text(encoding="utf-8")
        # Each migration + its bookkeeping row commit together atomically.
        with conn:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                (version, _dt.datetime.now(tz=_dt.UTC).isoformat()),
            )
        newly_applied.append(version)

    return newly_applied


def migrations_at_head(
    conn: sqlite3.Connection,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> bool:
    """True when every discovered migration has been applied."""
    discovered = {v for v, _ in discover_migrations(migrations_dir)}
    return discovered.issubset(applied_versions(conn))
