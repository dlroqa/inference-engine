"""Backup/restore of the database + config (Block 9)."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from engine.auth.keys import KeyStore
from engine.backup import (
    DB_ARCNAME,
    MANIFEST_ARCNAME,
    create_backup,
    restore_backup,
)
from engine.config import Settings
from engine.store.db import connect
from engine.store.migrations import apply_migrations


def _seed(settings: Settings, label: str) -> str:
    conn = connect(settings.db_path)  # type: ignore[arg-type]
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    record, _ = KeyStore(settings.db_path).create(label=label)  # type: ignore[arg-type]
    return record.id


def test_backup_restore_roundtrip(tmp_path: Path) -> None:
    src = Settings(data_dir=tmp_path / "src")
    key_id = _seed(src, "k1")
    archive = create_backup(src, tmp_path / "out")
    assert archive.is_file() and archive.suffix == ".gz"

    dst = Settings(data_dir=tmp_path / "dst")
    result = restore_backup(archive, dst)
    assert result.db_path == Path(dst.db_path)  # type: ignore[arg-type]
    restored = KeyStore(dst.db_path).list()  # type: ignore[arg-type]
    assert [r.id for r in restored] == [key_id]


def test_backup_includes_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('model_id = "from-config"\n')
    src = Settings(data_dir=tmp_path / "src", config_file=cfg)
    _seed(src, "k")
    archive = create_backup(src, tmp_path / "b.tar.gz")
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "config.toml" in names

    dst = Settings(data_dir=tmp_path / "dst")
    result = restore_backup(archive, dst, config_out=tmp_path / "restored.toml")
    assert result.config_path == tmp_path / "restored.toml"
    assert "from-config" in (tmp_path / "restored.toml").read_text()


def test_restore_refuses_overwrite_without_force(tmp_path: Path) -> None:
    src = Settings(data_dir=tmp_path / "src")
    _seed(src, "k")
    archive = create_backup(src, tmp_path / "b.tar.gz")

    dst = Settings(data_dir=tmp_path / "dst")
    _seed(dst, "existing")
    with pytest.raises(FileExistsError):
        restore_backup(archive, dst)
    # With force it replaces the DB.
    restore_backup(archive, dst, force=True)
    labels = [r.label for r in KeyStore(dst.db_path).list()]  # type: ignore[arg-type]
    assert labels == ["k"]


def test_restore_detects_corrupt_db(tmp_path: Path) -> None:
    src = Settings(data_dir=tmp_path / "src")
    _seed(src, "k")
    archive = create_backup(src, tmp_path / "b.tar.gz")

    # Rebuild the archive with a tampered database but the original manifest.
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive) as tin:
        tin.extractall(tmp_path / "x", filter="data")
    (tmp_path / "x" / DB_ARCNAME).write_bytes(b"corrupt")
    with tarfile.open(tampered, "w:gz") as tout:
        tout.add(tmp_path / "x" / MANIFEST_ARCNAME, arcname=MANIFEST_ARCNAME)
        tout.add(tmp_path / "x" / DB_ARCNAME, arcname=DB_ARCNAME)

    dst = Settings(data_dir=tmp_path / "dst")
    with pytest.raises(ValueError, match="checksum mismatch"):
        restore_backup(tampered, dst)


def test_restore_rejects_unexpected_members(tmp_path: Path) -> None:
    evil = tmp_path / "evil.tar.gz"
    payload = tmp_path / "evil.txt"
    payload.write_text("x")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(payload, arcname="../escape.txt")
    dst = Settings(data_dir=tmp_path / "dst")
    with pytest.raises(ValueError, match="unexpected archive member"):
        restore_backup(evil, dst)


def test_backup_missing_db_errors(tmp_path: Path) -> None:
    src = Settings(data_dir=tmp_path / "empty")
    with pytest.raises(FileNotFoundError):
        create_backup(src, tmp_path / "out")
