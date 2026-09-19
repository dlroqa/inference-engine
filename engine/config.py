"""Layered application configuration.

Precedence (highest wins):

    CLI flags  >  IE_* environment variables  >  config file (TOML)  >  defaults

Built on ``pydantic-settings`` so every value is validated. Invalid
configuration fails early (at load time) with a useful message rather than at
first use. Storage/config/log paths default to per-user OS locations via
``platformdirs`` and are all overridable.
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_config_path, user_data_dir
from pydantic import Field, ValidationError, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

APP_NAME = "inference-engine"
LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]

# The TOML path is resolved dynamically per load (CLI/env can point elsewhere),
# so it is passed to ``settings_customise_sources`` through a ContextVar rather
# than a static ``model_config`` entry.
_toml_path_var: ContextVar[Path | None] = ContextVar("_toml_path", default=None)


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


def default_data_dir() -> Path:
    return Path(user_data_dir(APP_NAME, appauthor=False))


def default_config_file() -> Path:
    return Path(user_config_path(APP_NAME, appauthor=False)) / "config.toml"


class Settings(BaseSettings):
    """Validated engine settings for the Block 0 foundation.

    Only foundational settings exist here. Inference, auth, remote binding, TLS,
    and dashboard settings are deliberately absent until their owning blocks.
    """

    model_config = SettingsConfigDict(
        env_prefix="IE_",
        extra="forbid",
        validate_default=True,
    )

    # Network. Loopback-only by default; explicit LAN/public binding and TLS are
    # deferred to Block 3, so no remote-binding surface exists yet.
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # Observability.
    log_level: LogLevel = "INFO"

    # Storage paths (all overridable, resolved to safe per-user locations).
    data_dir: Path = Field(default_factory=default_data_dir)
    db_path: Path | None = None
    log_dir: Path | None = None

    # Records which config file (if any) contributed values; informational.
    config_file: Path | None = None

    @model_validator(mode="after")
    def _derive_paths(self) -> Settings:
        if self.db_path is None:
            self.db_path = self.data_dir / "inference_engine.db"
        if self.log_dir is None:
            self.log_dir = self.data_dir / "logs"
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Order = precedence: init (CLI) > env > TOML file.
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        toml_path = _toml_path_var.get()
        if toml_path is not None and toml_path.is_file():
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=toml_path))
        return tuple(sources)


def _resolve_config_file(config_file: Path | None) -> Path | None:
    """Resolve which TOML config file to read, if any.

    Priority: explicit ``config_file`` argument (from a CLI flag) > default
    per-user location. The ``IE_CONFIG_FILE`` environment variable is also
    honoured by the caller in :func:`load_config`.
    """
    if config_file is not None:
        return Path(config_file).expanduser()
    default = default_config_file()
    return default if default.is_file() else None


def load_config(
    cli_overrides: dict[str, Any] | None = None,
    config_file: Path | None = None,
) -> Settings:
    """Load settings from all layers with correct precedence.

    ``cli_overrides`` are the highest-precedence values (typically parsed CLI
    flags); ``None`` values are ignored so unset flags do not clobber lower
    layers. Raises :class:`ConfigError` with a readable message on invalid
    configuration.
    """
    import os

    overrides = {k: v for k, v in (cli_overrides or {}).items() if v is not None}

    env_config_file = os.environ.get("IE_CONFIG_FILE")
    chosen = config_file
    if chosen is None and env_config_file:
        chosen = Path(env_config_file).expanduser()

    resolved_toml = _resolve_config_file(chosen)
    token = _toml_path_var.set(resolved_toml)
    try:
        settings = Settings(**overrides)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc, resolved_toml)) from exc
    finally:
        _toml_path_var.reset(token)

    if resolved_toml is not None and settings.config_file is None:
        settings.config_file = resolved_toml
    return settings


def _format_validation_error(exc: ValidationError, toml_path: Path | None) -> str:
    lines = ["Invalid configuration:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    if toml_path is not None:
        lines.append(f"Config file in effect: {toml_path}")
    lines.append("Precedence: CLI flags > IE_* env vars > config file > defaults.")
    return "\n".join(lines)
