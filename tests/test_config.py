"""Configuration: defaults, precedence, and early failure (Block 0 required)."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import ConfigError, Settings, load_config


def test_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_config(cli_overrides={"data_dir": tmp_path})
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.log_level == "INFO"
    # Derived paths.
    assert settings.db_path == tmp_path / "inference_engine.db"
    assert settings.log_dir == tmp_path / "logs"


def test_env_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IE_PORT", "9100")
    settings = load_config(cli_overrides={"data_dir": tmp_path})
    assert settings.port == 9100


def test_file_overrides_default(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('port = 8123\nlog_level = "WARNING"\n', encoding="utf-8")
    settings = load_config(cli_overrides={"data_dir": tmp_path}, config_file=cfg)
    assert settings.port == 8123
    assert settings.log_level == "WARNING"


def test_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("port = 8123\n", encoding="utf-8")
    monkeypatch.setenv("IE_PORT", "9200")
    settings = load_config(cli_overrides={"data_dir": tmp_path}, config_file=cfg)
    assert settings.port == 9200  # env beats file


def test_cli_overrides_env_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("port = 8123\n", encoding="utf-8")
    monkeypatch.setenv("IE_PORT", "9200")
    settings = load_config(cli_overrides={"port": 9999, "data_dir": tmp_path}, config_file=cfg)
    assert settings.port == 9999  # CLI beats env and file


def test_none_cli_overrides_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IE_PORT", "9200")
    # An unset CLI flag (None) must not clobber the env value.
    settings = load_config(cli_overrides={"port": None, "data_dir": tmp_path})
    assert settings.port == 9200


def test_invalid_port_fails_early(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(cli_overrides={"port": 70000, "data_dir": tmp_path})
    assert "port" in str(excinfo.value)


def test_invalid_log_level_fails_early(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('log_level = "CHATTY"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(cli_overrides={"data_dir": tmp_path}, config_file=cfg)


def test_unknown_config_key_fails_early(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("bogus_key = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(cli_overrides={"data_dir": tmp_path}, config_file=cfg)


def test_config_file_recorded(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("port = 8123\n", encoding="utf-8")
    settings = load_config(cli_overrides={"data_dir": tmp_path}, config_file=cfg)
    assert settings.config_file == cfg


def test_env_config_file_is_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "from_env.toml"
    cfg.write_text("port = 8456\n", encoding="utf-8")
    monkeypatch.setenv("IE_CONFIG_FILE", str(cfg))
    settings = load_config(cli_overrides={"data_dir": tmp_path})
    assert settings.port == 8456


def test_explicit_paths_not_overridden(tmp_path: Path) -> None:
    db = tmp_path / "custom.db"
    settings = Settings(data_dir=tmp_path, db_path=db)
    assert settings.db_path == db
