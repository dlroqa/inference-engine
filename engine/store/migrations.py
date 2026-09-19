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
# Migration files must not manage transactions themselves; the runner wraps each
# migration and its bookkeeping row in a single transaction.
_TXN_KEYWORD_RE = re.compile(r"\b(BEGIN|COMMIT|ROLLBACK)\b", re.IGNORECASE)


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
    """Apply all pending migrations. Returns the versions newly applied.

    Each migration and its ``schema_migrations`` bookkeeping row are committed
    together in one explicit transaction. On any failure the transaction is
    rolled back so a partially-applied migration is never left unrecorded, and
    the migration re-runs cleanly on the next attempt.
    """
    already = applied_versions(conn)  # also ensures the tracking table exists
    newly_applied: list[str] = []

    for version, path in discover_migrations(migrations_dir):
        if version in already:
            continue
        sql = path.read_text(encoding="utf-8")
        if _TXN_KEYWORD_RE.search(sql):
            raise ValueError(
                f"Migration {path.name!r} must not contain BEGIN/COMMIT/ROLLBACK; "
                "the runner manages the transaction."
            )
        # ``executescript`` disregards isolation_level and commits any pending
        # transaction first, so transaction control must live in the script.
        # The version (\d{4}) and ISO timestamp contain no quotes, so inlining
        # them into the atomic BEGIN..COMMIT script is injection-safe.
        applied_at = _dt.datetime.now(tz=_dt.UTC).isoformat()
        script = (
            "BEGIN;\n"
            f"{sql}\n"
            "INSERT INTO schema_migrations (version, applied_at) "
            f"VALUES ('{version}', '{applied_at}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            conn.rollback()
            raise
        newly_applied.append(version)

    return newly_applied


def migrations_at_head(
    conn: sqlite3.Connection,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> bool:
    """True when every discovered migration has been applied."""
    discovered = {v for v, _ in discover_migrations(migrations_dir)}
    return discovered.issubset(applied_versions(conn))
