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
from pydantic import BaseModel, Field, ValidationError, model_validator
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


class RemoteWorkerSpec(BaseModel):
    """One additional remote backend in the routing pool (Block 10, sub-slice 3).

    Declared as ``[[remote_workers]]`` tables in config.toml. Each is an
    OpenAI-compatible remote (vLLM or SGLang) serving the same model_id as the
    primary backend; the registry routes each request to the least-busy healthy
    one. Secrets belong in api_key via a secure source, never committed.
    """

    model_config = {"extra": "forbid"}

    name: str
    kind: Literal["remote_vllm", "remote_sglang"] = "remote_vllm"
    base_url: str
    model: str
    api_key: str | None = None
    connect_timeout_s: float = Field(default=10.0, ge=0.1, le=600.0)
    read_timeout_s: float = Field(default=60.0, ge=0.1, le=3600.0)
    max_prestream_retries: int = Field(default=1, ge=0, le=10)
    context_length: int | None = Field(default=None, ge=8, le=1_048_576)
    tls_verify: bool = True
    max_in_flight: int = Field(default=8, ge=1, le=4096)
    prefix_cache: bool = True  # server maintains a prefix cache (gates affinity)
    kv_metrics: bool = True  # scrape /metrics for KV/prefix-cache stats
    cost_per_1k_input: float = Field(default=0.0, ge=0.0)  # token cost accounting (5b)
    cost_per_1k_output: float = Field(default=0.0, ge=0.0)


class VirtualModelSpec(BaseModel):
    """A named virtual model that maps a client-facing model name to a routing
    policy over the backend pool (Block 10, sub-slice 5a).

    Declared as ``[[virtual_models]]`` tables. ``policy`` is:

    * ``route``   — serve from any backend in ``backends`` (least-busy / affinity).
    * ``cascade`` — try each step in ``steps`` in order; the first step with an
      available backend serves it (admission-time fallback across steps).

    Backend names refer to the primary (``"primary"``) and ``remote_workers`` names.
    """

    model_config = {"extra": "forbid"}

    name: str
    policy: Literal["route", "cascade"] = "route"
    backends: list[str] = Field(default_factory=list)  # route: allowed set
    steps: list[list[str]] = Field(default_factory=list)  # cascade: ordered groups

    @model_validator(mode="after")
    def _check_policy(self) -> VirtualModelSpec:
        if self.policy == "route":
            if not self.backends:
                raise ValueError(f"virtual model {self.name!r} (route) needs non-empty 'backends'")
            if self.steps:
                raise ValueError(f"virtual model {self.name!r} (route) must not set 'steps'")
        else:  # cascade
            if not self.steps or not all(self.steps):
                raise ValueError(
                    f"virtual model {self.name!r} (cascade) needs 'steps' as non-empty groups"
                )
            if self.backends:
                raise ValueError(f"virtual model {self.name!r} (cascade) must not set 'backends'")
        return self

    def normalized_steps(self) -> list[list[str]]:
        """The policy as an ordered list of backend-name groups."""
        return [list(self.backends)] if self.policy == "route" else [list(g) for g in self.steps]


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

    # gRPC edge (Block 10, sub-slice 6). An independently secured/deployed gRPC
    # service on its own port, off by default. It reuses the same auth (API key
    # via call metadata) and pipeline as HTTP. Provide both TLS files for a
    # secure port; otherwise front it with a TLS-terminating proxy.
    grpc_enabled: bool = False
    grpc_host: str = "127.0.0.1"
    grpc_port: int = Field(default=50051, ge=0, le=65535)  # 0 = OS-assigned (ephemeral)
    grpc_tls_cert: Path | None = None
    grpc_tls_key: Path | None = None

    # Auth (Block 3). None = auto: required when not loopback-bound.
    require_auth: bool | None = None

    # Request limits (0 = unlimited). Applied per authenticated key.
    max_request_bytes: int = Field(default=2_000_000, ge=0)
    rate_limit_per_min: int = Field(default=120, ge=0)
    max_concurrent_per_key: int = Field(default=8, ge=0)

    # Controlled concurrency (Block 7). Engine-wide admission control in front of
    # the backend. The Tier-1 llama.cpp runtime serializes decoding per model
    # context, so the honest default concurrency is 1; overlapping requests wait
    # in a bounded queue (up to max_concurrency_queue waiters, concurrency_queue_timeout_s
    # seconds) and are rejected with a retriable saturation error beyond that.
    # See docs/concurrency.md before raising max_concurrency.
    max_concurrency: int = Field(default=1, ge=1, le=1024)
    max_concurrency_queue: int = Field(default=32, ge=0, le=100_000)
    concurrency_queue_timeout_s: float = Field(default=30.0, ge=0.0, le=3600.0)

    # Production deployment (Block 9). Readiness/drain knobs for load-balanced
    # deployments. require_model_ready makes /readyz report not-ready until a model
    # is loaded (so a balancer only routes once inference can serve);
    # drain_timeout_s bounds how long graceful shutdown waits for in-flight work.
    require_model_ready: bool = False
    drain_timeout_s: float = Field(default=30.0, ge=0.0, le=600.0)

    # Security hardening baseline (Block 9b). All default to the current
    # (permissive-but-safe) behavior so existing setups are unaffected; tighten
    # them for exposed/production or air-gapped deployments. See docs/security.md.
    #
    # ip_allowlist: when non-empty, only these CIDRs may reach the server (403
    # otherwise). trust_forwarded_for uses the client IP from X-Forwarded-For (only
    # enable behind a trusted reverse proxy that sets it).
    ip_allowlist: list[str] = Field(default_factory=list)
    trust_forwarded_for: bool = False
    # Kill switches for dynamic/unsafe features.
    allow_network_downloads: bool = True  # egress: model downloads/imports from HF/URL
    allow_model_management: bool = True  # dynamic import/download/load/unload/delete
    allow_structured_output: bool = True  # grammar/JSON-constrained decoding
    diagnostics_enabled: bool = True  # the /diagnostics support bundle

    @model_validator(mode="after")
    def _validate_ip_allowlist(self) -> Settings:
        import ipaddress

        for cidr in self.ip_allowlist:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid ip_allowlist entry {cidr!r}: {exc}") from exc
        return self

    # Compute-unit quota (Block 3). 0 = unlimited. 5-hour rolling window and a
    # weekly fixed cap; CU = prompt_tokens*w_in + completion_tokens*w_out.
    quota_5h_cu: float = Field(default=1_000_000.0, ge=0)
    quota_weekly_cu: float = Field(default=5_000_000.0, ge=0)
    cu_prompt_weight: float = Field(default=1.0, ge=0)
    cu_completion_weight: float = Field(default=1.0, ge=0)

    # Observability.
    log_level: LogLevel = "INFO"

    # Operability core (Block 4). Telemetry sampler interval and the bounded
    # buffers behind the event bus / recent-log mirror. 0 disables the sampler.
    metrics_interval_s: float = Field(default=1.0, ge=0.0, le=60.0)
    event_history_size: int = Field(default=200, ge=0, le=10_000)
    event_subscriber_queue: int = Field(default=500, ge=1, le=100_000)
    log_ring_size: int = Field(default=500, ge=0, le=100_000)
    log_events_max_rows: int = Field(default=2000, ge=0, le=1_000_000)

    # Backend selection (Block 10). "llamacpp" is the local GGUF runtime (Block 1);
    # "remote_vllm"/"remote_sglang" proxy generation to an external OpenAI-compatible
    # server (vLLM, sub-slice 1; SGLang, sub-slice 2). Remote adapters are gated by
    # allow_remote_backends and require remote_base_url + remote_model. See
    # docs/backends.md.
    backend_kind: Literal["llamacpp", "remote_vllm", "remote_sglang"] = "llamacpp"

    # Remote backend (Block 10, sub-slice 1 = vLLM). Credentials are sent only to
    # remote_base_url and never logged. remote_model is the explicit upstream model
    # name (the engine's model_id is what clients see). 0 retries = fail fast; any
    # pre-stream retry is bounded and never happens after the first token is sent.
    allow_remote_backends: bool = True  # kill switch for outbound remote inference
    remote_base_url: str | None = None
    remote_model: str | None = None
    remote_api_key: str | None = None
    remote_connect_timeout_s: float = Field(default=10.0, ge=0.1, le=600.0)
    remote_read_timeout_s: float = Field(default=60.0, ge=0.1, le=3600.0)
    remote_max_prestream_retries: int = Field(default=1, ge=0, le=10)
    remote_context_length: int | None = Field(default=None, ge=8, le=1_048_576)
    remote_tls_verify: bool = True
    remote_prefix_cache: bool = True  # primary remote maintains a prefix cache
    remote_kv_metrics: bool = True  # scrape the primary remote's /metrics

    # Multi-backend routing (Block 10, sub-slice 3). The primary backend above is
    # entry 0; remote_workers add more OpenAI-compatible backends to the pool.
    # Requests route to the least-busy healthy backend. backend_health_interval_s
    # governs the background liveness probe of remote backends (0 disables it).
    # primary_max_in_flight caps concurrent generations placed on the primary
    # (llama.cpp serializes, so 1; raise it for a remote primary). See docs/backends.md.
    remote_workers: list[RemoteWorkerSpec] = Field(default_factory=list)
    # Named virtual auto-models (Block 10, sub-slice 5a): map a client-facing
    # model name to a route/cascade policy over the pool. See docs/backends.md.
    virtual_models: list[VirtualModelSpec] = Field(default_factory=list)
    # External-provider spillover (Block 10, sub-slice 7). External providers are
    # OpenAI-compatible endpoints used ONLY as overflow when the local pool cannot
    # admit a request. Egress is off by default: enable allow_external_providers
    # AND list their hosts in egress_allowlist, or startup fails. See docs/backends.md.
    allow_external_providers: bool = False
    egress_allowlist: list[str] = Field(default_factory=list)
    external_providers: list[RemoteWorkerSpec] = Field(default_factory=list)
    primary_max_in_flight: int = Field(default=1, ge=1, le=4096)
    backend_health_interval_s: float = Field(default=10.0, ge=0.0, le=3600.0)
    # Prefix-affinity routing (Block 10.4): route requests sharing the leading
    # N characters of their prompt to the same prefix-cache-capable backend to
    # reuse its cache. 0 disables it (pure least-busy). Only affects backends
    # that report supports_prefix_cache; others always use least-busy.
    prefix_affinity_chars: int = Field(default=0, ge=0, le=100_000)
    # Per-route cost accounting (Block 10.5b): token cost weights for the primary
    # backend (0 = free, e.g. a local model). Remote workers set their own.
    primary_cost_per_1k_input: float = Field(default=0.0, ge=0.0)
    primary_cost_per_1k_output: float = Field(default=0.0, ge=0.0)

    # Local inference runtime (Block 1). A single explicitly configured GGUF
    # model. Multiple models, downloads, and GPU auto-tuning are later blocks;
    # n_gpu_layers is a manual knob (0 = CPU-only default).
    model_path: Path | None = None
    model_id: str = "local-model"
    n_ctx: int = Field(default=4096, ge=8, le=1_048_576)
    n_threads: int | None = Field(default=None, ge=1)
    n_gpu_layers: int = Field(default=0, ge=0)

    # Model lifecycle (Block 6). The registry stores GGUF models under models_dir;
    # downloads are checksum-verified and resumable. hf_endpoint is overridable for
    # mirrors/enterprise Hugging Face. 0 = unlimited download size.
    models_dir: Path | None = None
    hf_endpoint: str = "https://huggingface.co"
    download_chunk_bytes: int = Field(default=1_048_576, ge=8192)
    max_model_bytes: int = Field(default=0, ge=0)

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
        if self.models_dir is None:
            self.models_dir = self.data_dir / "models"
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

    @model_validator(mode="after")
    def _validate_remote_backend(self) -> Settings:
        if self.backend_kind.startswith("remote_"):
            missing = [
                name
                for name, value in (
                    ("remote_base_url", self.remote_base_url),
                    ("remote_model", self.remote_model),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"backend_kind={self.backend_kind!r} requires "
                    + " and ".join(missing)
                    + " (set IE_REMOTE_BASE_URL / IE_REMOTE_MODEL); see docs/backends.md"
                )
        return self

    @model_validator(mode="after")
    def _validate_external_providers(self) -> Settings:
        if not self.external_providers:
            return self
        if not self.allow_external_providers:
            raise ValueError(
                "external_providers are configured but allow_external_providers is false; "
                "external egress is off by default — set allow_external_providers=true to "
                "acknowledge sending prompts off-premise (see docs/backends.md)"
            )
        if not self.egress_allowlist:
            raise ValueError(
                "external_providers require a non-empty egress_allowlist (the hosts they "
                "may reach); refusing to allow unrestricted outbound inference"
            )
        for prov in self.external_providers:
            host = _url_host(prov.base_url)
            if host is None or not _host_allowed(host, self.egress_allowlist):
                raise ValueError(
                    f"external provider {prov.name!r} base_url host {host!r} is not in "
                    f"egress_allowlist {self.egress_allowlist}"
                )
        return self

    @model_validator(mode="after")
    def _validate_grpc(self) -> Settings:
        if not self.grpc_enabled:
            return self
        if (self.grpc_tls_cert is None) != (self.grpc_tls_key is None):
            raise ValueError("grpc_tls_cert and grpc_tls_key must be set together")
        if self.grpc_host not in {"127.0.0.1", "::1", "localhost"} and not self.allow_network_bind:
            raise ValueError(
                f"refusing to bind gRPC to non-loopback host {self.grpc_host!r} without "
                "allow_network_bind=true; review the TLS guidance in docs/grpc.md"
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


def _url_host(url: str) -> str | None:
    from urllib.parse import urlparse

    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def _host_allowed(host: str, allowlist: list[str]) -> bool:
    """True if ``host`` matches an allowlist entry (exact or dot-suffix domain)."""
    host = host.lower()
    for raw in allowlist:
        entry = raw.lower().lstrip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return False


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
