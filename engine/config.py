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

    # Network. Loopback-only by default. Binding to a non-loopback interface
    # requires an explicit opt-in (allow_network_bind) and, unless
    # allow_insecure_bind is set, authentication.
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    allow_network_bind: bool = False
    allow_insecure_bind: bool = False

    # Auth (Block 3). None = auto: required when not loopback-bound.
    require_auth: bool | None = None

    # Request limits (0 = unlimited). Applied per authenticated key.
    max_request_bytes: int = Field(default=2_000_000, ge=0)
    rate_limit_per_min: int = Field(default=120, ge=0)
    max_concurrent_per_key: int = Field(default=8, ge=0)

    # Compute-unit quota (Block 3). 0 = unlimited. 5-hour rolling window and a
    # weekly fixed cap; CU = prompt_tokens*w_in + completion_tokens*w_out.
    quota_5h_cu: float = Field(default=1_000_000.0, ge=0)
    quota_weekly_cu: float = Field(default=5_000_000.0, ge=0)
    cu_prompt_weight: float = Field(default=1.0, ge=0)
    cu_completion_weight: float = Field(default=1.0, ge=0)

    # Observability.
    log_level: LogLevel = "INFO"

    # Local inference runtime (Block 1). A single explicitly configured GGUF
    # model. Multiple models, downloads, and GPU auto-tuning are later blocks;
    # n_gpu_layers is a manual knob (0 = CPU-only default).
    model_path: Path | None = None
    model_id: str = "local-model"
    n_ctx: int = Field(default=4096, ge=8, le=1_048_576)
    n_threads: int | None = Field(default=None, ge=1)
    n_gpu_layers: int = Field(default=0, ge=0)

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

    @model_validator(mode="after")
    def _validate_binding(self) -> Settings:
        if not self.is_loopback_host():
            if not self.allow_network_bind:
                raise ValueError(
                    f"refusing to bind to non-loopback host {self.host!r} without an "
                    "explicit choice; set allow_network_bind=true (IE_ALLOW_NETWORK_BIND) "
                    "and review the TLS/reverse-proxy guidance in the README"
                )
            if not self.effective_require_auth() and not self.allow_insecure_bind:
                raise ValueError(
                    f"refusing to bind to non-loopback host {self.host!r} with auth "
                    "disabled; enable require_auth or set allow_insecure_bind=true to "
                    "acknowledge the risk"
                )
        return self

    def is_loopback_host(self) -> bool:
        return self.host in {"127.0.0.1", "::1", "localhost"}

    def effective_require_auth(self) -> bool:
        """Whether inference endpoints require an API key.

        Explicit ``require_auth`` wins; otherwise auth is required whenever the
        server is not strictly loopback-bound.
        """
        if self.require_auth is not None:
            return self.require_auth
        return not self.is_loopback_host()

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


def load_config(
    cli_overrides: dict[str, Any] | None = None,
    config_file: Path | None = None,
) -> Settings:
    """Load settings from all layers with correct precedence.

    ``cli_overrides`` are the highest-precedence values (typically parsed CLI
    flags); ``None`` values are ignored so unset flags do not clobber lower
    layers. Raises :class:`ConfigError` with a readable message on invalid
    configuration.

    An *explicitly* requested config file (via the ``config_file`` argument or
    the ``IE_CONFIG_FILE`` environment variable) that does not exist is a fatal
    error — misconfiguration is surfaced now, not at runtime. Only the default
    per-user config location is allowed to be absent.
    """
    import os

    overrides = {k: v for k, v in (cli_overrides or {}).items() if v is not None}

    env_config_file = os.environ.get("IE_CONFIG_FILE")
    explicit = config_file
    if explicit is None and env_config_file:
        explicit = Path(env_config_file)

    resolved_toml: Path | None
    if explicit is not None:
        resolved_toml = Path(explicit).expanduser()
        if not resolved_toml.is_file():
            raise ConfigError(
                f"Config file not found: {resolved_toml}\n"
                "The path was requested explicitly (via --config or IE_CONFIG_FILE)."
            )
    else:
        default = default_config_file()
        resolved_toml = default if default.is_file() else None

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
