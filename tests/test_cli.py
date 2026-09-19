"""CLI smoke tests: version, config, migrate, and invalid-config exit code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine import __version__
from engine.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["version"])
    assert rc == 0
    assert __version__ in capsys.readouterr().out


def test_config_prints_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["config", "--data-dir", str(tmp_path)])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["port"] == 8000


def test_migrate_applies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "cli.db"
    rc = main(["migrate", "--data-dir", str(tmp_path), "--db-path", str(db)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "0001" in out["newly_applied"]
    assert db.exists()


def test_invalid_config_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["config", "--data-dir", str(tmp_path), "--port", "70000"])
    assert rc == 2
    assert "port" in capsys.readouterr().err
