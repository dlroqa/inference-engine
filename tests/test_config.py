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


def test_loopback_defaults(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path)
    assert s.is_loopback_host() is True
    assert s.effective_require_auth() is False


def test_non_loopback_requires_explicit_opt_in(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(cli_overrides={"data_dir": tmp_path, "host": "0.0.0.0"})
    assert "non-loopback" in str(excinfo.value)


def test_non_loopback_with_opt_in_requires_auth_by_default(tmp_path: Path) -> None:
    # allow_network_bind alone is fine because auth is then required by default.
    s = Settings(data_dir=tmp_path, host="0.0.0.0", allow_network_bind=True)
    assert s.effective_require_auth() is True


def test_non_loopback_auth_disabled_needs_insecure_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IE_HOST", "0.0.0.0")
    monkeypatch.setenv("IE_ALLOW_NETWORK_BIND", "true")
    monkeypatch.setenv("IE_REQUIRE_AUTH", "false")
    with pytest.raises(ConfigError):
        load_config(cli_overrides={"data_dir": tmp_path})
    monkeypatch.setenv("IE_ALLOW_INSECURE_BIND", "true")
    s = load_config(cli_overrides={"data_dir": tmp_path})
    assert s.host == "0.0.0.0"
    assert s.effective_require_auth() is False


def test_missing_explicit_config_file_fails(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.toml"
    with pytest.raises(ConfigError) as excinfo:
        load_config(cli_overrides={"data_dir": tmp_path}, config_file=missing)
    assert "not found" in str(excinfo.value)


def test_missing_env_config_file_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "nope.toml"
    monkeypatch.setenv("IE_CONFIG_FILE", str(missing))
    with pytest.raises(ConfigError):
        load_config(cli_overrides={"data_dir": tmp_path})


def test_missing_default_config_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No explicit file requested: absence of the default location is fine.
    monkeypatch.setattr("engine.config.default_config_file", lambda: tmp_path / "absent.toml")
    settings = load_config(cli_overrides={"data_dir": tmp_path})
    assert settings.config_file is None
    assert settings.port == 8000


def test_invalid_ip_allowlist_rejected() -> None:
    import pytest

    from engine.config import Settings

    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        Settings(data_dir=None, ip_allowlist=["not-a-cidr"])  # type: ignore[arg-type]


def test_valid_ip_allowlist_accepted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from engine.config import Settings

    s = Settings(data_dir=tmp_path, ip_allowlist=["127.0.0.1/32", "10.0.0.0/8", "::1/128"])
    assert len(s.ip_allowlist) == 3


def test_remote_workers_parse_and_default_capacity(tmp_path):
    from engine.config import Settings

    s = Settings(
        data_dir=tmp_path,
        remote_workers=[
            {"name": "w1", "kind": "remote_vllm", "base_url": "http://a/v1", "model": "m"},
            {
                "name": "w2",
                "kind": "remote_sglang",
                "base_url": "http://b",
                "model": "m",
                "max_in_flight": 16,
            },
        ],
    )
    assert [w.name for w in s.remote_workers] == ["w1", "w2"]
    assert s.remote_workers[0].max_in_flight == 8  # default
    assert s.remote_workers[1].max_in_flight == 16
    assert s.remote_workers[1].kind == "remote_sglang"


def test_remote_worker_rejects_unknown_kind(tmp_path):
    from pydantic import ValidationError

    from engine.config import Settings

    with __import__("pytest").raises(ValidationError):
        Settings(
            data_dir=tmp_path,
            remote_workers=[
                {"name": "w", "kind": "llamacpp", "base_url": "http://a", "model": "m"}
            ],
        )


def test_virtual_model_route_and_cascade_normalize(tmp_path):
    from engine.config import Settings

    s = Settings(
        data_dir=tmp_path,
        virtual_models=[
            {"name": "fast", "policy": "route", "backends": ["primary"]},
            {"name": "tiered", "policy": "cascade", "steps": [["primary"], ["w1", "w2"]]},
        ],
    )
    assert s.virtual_models[0].normalized_steps() == [["primary"]]
    assert s.virtual_models[1].normalized_steps() == [["primary"], ["w1", "w2"]]


def test_virtual_model_policy_validation(tmp_path):
    from pydantic import ValidationError

    from engine.config import Settings

    with __import__("pytest").raises(ValidationError):  # route needs backends
        Settings(data_dir=tmp_path, virtual_models=[{"name": "x", "policy": "route"}])
    with __import__("pytest").raises(ValidationError):  # cascade must not set backends
        Settings(
            data_dir=tmp_path,
            virtual_models=[{"name": "x", "policy": "cascade", "backends": ["a"]}],
        )


def test_cost_weights_parse_and_reject_negative(tmp_path):
    from pydantic import ValidationError

    from engine.config import RemoteWorkerSpec, Settings

    s = Settings(data_dir=tmp_path, primary_cost_per_1k_input=0.5, primary_cost_per_1k_output=1.5)
    assert (s.primary_cost_per_1k_input, s.primary_cost_per_1k_output) == (0.5, 1.5)
    w = RemoteWorkerSpec(name="w", base_url="http://x/v1", model="m", cost_per_1k_input=2.0)
    assert w.cost_per_1k_input == 2.0
    with __import__("pytest").raises(ValidationError):
        Settings(data_dir=tmp_path, primary_cost_per_1k_input=-1.0)
