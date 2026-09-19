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


def test_generate_streams_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import engine.inference as inference
    from tests.support.fake_backend import FakeBackend

    monkeypatch.setattr(inference, "build_backend", lambda settings: FakeBackend(tokens=["2", "2"]))
    model = tmp_path / "m.gguf"
    model.write_bytes(b"x")
    rc = main(
        [
            "generate",
            "--data-dir",
            str(tmp_path),
            "--model-path",
            str(model),
            "--prompt",
            "hi there",
            "--max-tokens",
            "4",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "22" in captured.out  # streamed tokens
    assert '"finish_reason": "stop"' in captured.err  # result summary


def test_serve_invokes_uvicorn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    calls: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        calls["host"] = kwargs.get("host")
        calls["port"] = kwargs.get("port")

    monkeypatch.setattr(uvicorn, "run", fake_run)
    rc = main(["serve", "--data-dir", str(tmp_path), "--host", "127.0.0.1", "--port", "8099"])
    assert rc == 0
    assert calls == {"host": "127.0.0.1", "port": 8099}
