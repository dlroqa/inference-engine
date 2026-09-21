"""The model registry: persistent record of known GGUF models.

One row per imported or downloaded model, tracking its on-disk path, verified
checksum, probed GGUF metadata, download progress, status, and whether it is the
active model. Backed by SQLite with short-lived connections (consistent with the
key and usage stores).
"""

from __future__ import annotations

import datetime as _dt
import enum
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.store.db import connect


class ModelStatus(enum.StrEnum):
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    READY = "ready"
    ERROR = "error"
    CANCELLED = "cancelled"


def _now() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat()


@dataclass(slots=True)
class ModelRecord:
    id: str
    name: str
    filename: str
    path: str
    source_type: str
    source_ref: str | None
    sha256: str | None
    expected_sha256: str | None
    size_bytes: int | None
    downloaded_bytes: int
    quant: str | None
    arch: str | None
    context_length: int | None
    status: str
    error: str | None
    active: bool
    added_at: str
    updated_at: str | None

    @property
    def progress(self) -> float | None:
        if not self.size_bytes:
            return None
        return min(1.0, self.downloaded_bytes / self.size_bytes)


def _row(row: sqlite3.Row) -> ModelRecord:
    return ModelRecord(
        id=row["id"],
        name=row["name"],
        filename=row["filename"],
        path=row["path"],
        source_type=row["source_type"],
        source_ref=row["source_ref"],
        sha256=row["sha256"],
        expected_sha256=row["expected_sha256"],
        size_bytes=row["size_bytes"],
        downloaded_bytes=row["downloaded_bytes"],
        quant=row["quant"],
        arch=row["arch"],
        context_length=row["context_length"],
        status=row["status"],
        error=row["error"],
        active=bool(row["active"]),
        added_at=row["added_at"],
        updated_at=row["updated_at"],
    )


class ModelRegistry:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def create(
        self,
        *,
        name: str,
        filename: str,
        path: Path,
        source_type: str,
        source_ref: str | None,
        status: ModelStatus,
        expected_sha256: str | None = None,
        sha256: str | None = None,
        size_bytes: int | None = None,
        quant: str | None = None,
        arch: str | None = None,
        context_length: int | None = None,
    ) -> ModelRecord:
        model_id = uuid.uuid4().hex
        now = _now()
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO models (id, name, filename, path, source_type, source_ref, "
                    " sha256, expected_sha256, size_bytes, quant, arch, context_length, "
                    " status, added_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    (
                        model_id,
                        name,
                        filename,
                        str(path),
                        source_type,
                        source_ref,
                        sha256,
                        expected_sha256,
                        size_bytes,
                        quant,
                        arch,
                        context_length,
                        str(status),
                        now,
                        now,
                    ),
                )
        finally:
            conn.close()
        got = self.get(model_id)
        assert got is not None
        return got

    def update(self, model_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        columns = ", ".join(f"{k} = ?" for k in fields)
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    f"UPDATE models SET {columns} WHERE id = ?;",
                    (*fields.values(), model_id),
                )
        finally:
            conn.close()

    def add_progress(self, model_id: str, downloaded_bytes: int) -> None:
        """Set the downloaded-byte counter (called during a download)."""
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE models SET downloaded_bytes = ?, updated_at = ? WHERE id = ?;",
                    (downloaded_bytes, _now(), model_id),
                )
        finally:
            conn.close()

    def get(self, model_id: str) -> ModelRecord | None:
        conn = connect(self._db_path)
        try:
            row = conn.execute("SELECT * FROM models WHERE id = ?;", (model_id,)).fetchone()
            return _row(row) if row is not None else None
        finally:
            conn.close()

    def list(self) -> list[ModelRecord]:
        conn = connect(self._db_path)
        try:
            rows = conn.execute("SELECT * FROM models ORDER BY added_at;").fetchall()
            return [_row(r) for r in rows]
        finally:
            conn.close()

    def get_active(self) -> ModelRecord | None:
        conn = connect(self._db_path)
        try:
            row = conn.execute("SELECT * FROM models WHERE active = 1;").fetchone()
            return _row(row) if row is not None else None
        finally:
            conn.close()

    def find_by_path(self, path: Path) -> ModelRecord | None:
        conn = connect(self._db_path)
        try:
            row = conn.execute("SELECT * FROM models WHERE path = ?;", (str(path),)).fetchone()
            return _row(row) if row is not None else None
        finally:
            conn.close()

    def set_active(self, model_id: str) -> None:
        """Make ``model_id`` the sole active model (atomic)."""
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute("UPDATE models SET active = 0 WHERE active = 1;")
                conn.execute(
                    "UPDATE models SET active = 1, updated_at = ? WHERE id = ?;",
                    (_now(), model_id),
                )
        finally:
            conn.close()

    def clear_active(self) -> None:
        conn = connect(self._db_path)
        try:
            with conn:
                conn.execute("UPDATE models SET active = 0 WHERE active = 1;")
        finally:
            conn.close()

    def delete(self, model_id: str) -> bool:
        conn = connect(self._db_path)
        try:
            with conn:
                cur = conn.execute("DELETE FROM models WHERE id = ?;", (model_id,))
            return cur.rowcount > 0
        finally:
            conn.close()
