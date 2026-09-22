"""Backup and restore for the operator-critical state (Block 9).

Backs up the SQLite database (registry, API keys, usage, logs) and the active
config file into a single checksum-verified tarball. **Model files are not
included** — they are large, content-addressed, and re-downloadable; back them up
separately (see docs/deployment.md). Restore rebuilds a working
registry/configuration state without manual database surgery.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from engine import __version__
from engine.config import Settings

DB_ARCNAME = "inference_engine.db"
CONFIG_ARCNAME = "config.toml"
MANIFEST_ARCNAME = "manifest.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _consistent_db_copy(db_path: Path, dest: Path) -> None:
    """Copy a live SQLite DB consistently using the online backup API."""
    src = sqlite3.connect(str(db_path))
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def create_backup(settings: Settings, out: Path) -> Path:
    """Write a backup tarball and return its path.

    ``out`` may be a directory (a timestamped filename is generated) or a
    ``.tar.gz`` file path.
    """
    db_path = Path(settings.db_path)  # type: ignore[arg-type]
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")

    if out.is_dir() or out.suffix not in (".gz", ".tgz"):
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        out.mkdir(parents=True, exist_ok=True)
        archive = out / f"inference-engine-backup-{ts}.tar.gz"
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        archive = out

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        db_copy = tmpdir / DB_ARCNAME
        _consistent_db_copy(db_path, db_copy)

        manifest: dict[str, object] = {
            "engine_version": __version__,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "db_filename": DB_ARCNAME,
            "db_sha256": _sha256(db_copy),
            "config_included": False,
            "note": "model files are not included; back them up separately",
        }

        config_copy: Path | None = None
        config_file = settings.config_file
        if config_file is not None and Path(config_file).is_file():
            config_copy = tmpdir / CONFIG_ARCNAME
            config_copy.write_bytes(Path(config_file).read_bytes())
            manifest["config_included"] = True
            manifest["config_sha256"] = _sha256(config_copy)

        manifest_path = tmpdir / MANIFEST_ARCNAME
        manifest_path.write_text(json.dumps(manifest, indent=2))

        with tarfile.open(archive, "w:gz") as tar:
            tar.add(manifest_path, arcname=MANIFEST_ARCNAME)
            tar.add(db_copy, arcname=DB_ARCNAME)
            if config_copy is not None:
                tar.add(config_copy, arcname=CONFIG_ARCNAME)

    return archive


@dataclass(slots=True)
class RestoreResult:
    db_path: Path
    config_path: Path | None
    manifest: dict[str, object]


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Only allow the known, flat archive members (no traversal, no absolute)."""
    allowed = {MANIFEST_ARCNAME, DB_ARCNAME, CONFIG_ARCNAME}
    members = []
    for m in tar.getmembers():
        if m.name not in allowed or not m.isfile():
            raise ValueError(f"unexpected archive member: {m.name!r}")
        members.append(m)
    return members


def restore_backup(
    archive: Path, settings: Settings, *, force: bool = False, config_out: Path | None = None
) -> RestoreResult:
    """Restore a backup tarball into the configured locations.

    Verifies the manifest checksums before writing anything. Refuses to overwrite
    an existing database unless ``force`` is set. Returns a :class:`RestoreResult`.
    """
    if not archive.is_file():
        raise FileNotFoundError(f"backup archive not found: {archive}")
    db_path = Path(settings.db_path)  # type: ignore[arg-type]
    if db_path.exists() and not force:
        raise FileExistsError(
            f"refusing to overwrite existing database {db_path}; pass force=True to replace it"
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            members = _safe_members(tar)
            # Members are validated above (flat, known names); ``data`` filter adds
            # defense-in-depth against path traversal / special files.
            tar.extractall(tmpdir, members=members, filter="data")

        manifest_path = tmpdir / MANIFEST_ARCNAME
        if not manifest_path.is_file():
            raise ValueError("backup is missing manifest.json")
        manifest = json.loads(manifest_path.read_text())

        db_copy = tmpdir / DB_ARCNAME
        if not db_copy.is_file():
            raise ValueError("backup is missing the database file")
        if _sha256(db_copy) != manifest.get("db_sha256"):
            raise ValueError("database checksum mismatch; backup is corrupt")

        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(db_copy.read_bytes())

        restored_config: Path | None = None
        config_copy = tmpdir / CONFIG_ARCNAME
        if config_copy.is_file():
            if _sha256(config_copy) != manifest.get("config_sha256"):
                raise ValueError("config checksum mismatch; backup is corrupt")
            target = config_out or (
                Path(settings.config_file)
                if settings.config_file
                else db_path.parent / CONFIG_ARCNAME
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(config_copy.read_bytes())
            restored_config = target

    return RestoreResult(db_path=db_path, config_path=restored_config, manifest=manifest)
