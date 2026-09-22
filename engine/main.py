"""FastAPI application factory and lifecycle.

Wires configuration, structured logging, SQLite/WAL migrations, health endpoints
(Block 0), and the OpenAI-compatible API edge over the internal inference backend
(Blocks 1–2). The API edge never calls llama.cpp directly — it translates onto the
internal ``GenerationRequest``/token-stream contract.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from engine import __version__
from engine.api import health
from engine.api.admin_router import router as admin_router
from engine.api.anthropic_router import router as anthropic_router
from engine.api.errors import error_response, register_exception_handlers
from engine.api.models_router import router as models_router
from engine.api.openai_router import router as openai_router
from engine.api.ops_router import router as ops_router
from engine.api.ws_router import router as ws_router
from engine.audit import AuditLog
from engine.auth.keys import KeyStore
from engine.config import Settings, load_config
from engine.gateway import Gateway
from engine.inference.base import InferenceBackend
from engine.inference.factory import build_backend
from engine.inference.scheduler import Scheduler
from engine.inference.types import BackendError
from engine.logging_setup import configure_logging, get_logger
from engine.models.registry import ModelRegistry
from engine.models.service import ModelService
from engine.quota.store import UsageStore
from engine.store.db import connect
from engine.store.migrations import apply_migrations, migrations_at_head
from engine.telemetry.counters import Counters
from engine.telemetry.events import EventBus
from engine.telemetry.logbuffer import LogCollector
from engine.telemetry.service import Telemetry

# The active settings for the running app. Set by ``create_app`` so request
# handlers (e.g. readiness) can access configuration without a DI framework.
_active_settings: Settings | None = None


def get_settings() -> Settings:
    if _active_settings is None:
        raise RuntimeError("Application settings are not initialized")
    return _active_settings


def _client_ip(request: Request, *, trust_forwarded_for: bool) -> str | None:
    """Resolve the caller's IP for the trusted-network policy.

    Uses the direct peer by default. Only when ``trust_forwarded_for`` is set (i.e.
    the app sits behind a trusted reverse proxy) is the left-most ``X-Forwarded-For``
    entry used; otherwise a client could spoof the header to bypass the allowlist.
    """
    if trust_forwarded_for:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _ip_permitted(
    client_ip: str, networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def create_app(
    settings: Settings | None = None,
    backend: InferenceBackend | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    If ``settings`` is not supplied it is loaded from the standard layered
    sources. If ``backend`` is supplied it is used as-is (tests inject a fake);
    otherwise, when a model is configured, a backend is built and loaded on
    startup. The app always starts even if no model is configured or loading
    fails — the ``/v1`` endpoints then report ``503 model_not_loaded``.
    """
    global _active_settings
    settings = settings or load_config()
    _active_settings = settings
    injected_backend = backend is not None

    configure_logging(settings.log_level)
    log = get_logger("engine.startup")

    # Operability core (Block 4): event bus, counters, telemetry service, and a
    # bounded structured-log mirror installed on the root logger.
    event_bus = EventBus(
        history=settings.event_history_size,
        subscriber_queue=settings.event_subscriber_queue,
    )
    counters = Counters()
    telemetry = Telemetry(
        bus=event_bus,
        counters=counters,
        data_dir=settings.data_dir,
        sample_interval_s=settings.metrics_interval_s,
    )
    log_collector = LogCollector(
        settings.db_path,
        ring_size=settings.log_ring_size,
        max_rows=settings.log_events_max_rows,
    )
    logging.getLogger().addHandler(log_collector)

    # Controlled concurrency (Block 7): admission control in front of the backend.
    scheduler = Scheduler(
        max_concurrency=settings.max_concurrency,
        max_queue_depth=settings.max_concurrency_queue,
        queue_timeout_s=settings.concurrency_queue_timeout_s,
    )
    telemetry.scheduler = scheduler

    # Model lifecycle (Block 6): registry + orchestration service.
    model_registry = ModelRegistry(settings.db_path)  # type: ignore[arg-type]
    model_service = ModelService(settings, model_registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "startup",
            extra={
                "version": __version__,
                "host": settings.host,
                "port": settings.port,
                "db_path": str(settings.db_path),
                "data_dir": str(settings.data_dir),
            },
        )
        conn = connect(settings.db_path)  # type: ignore[arg-type]
        try:
            applied = apply_migrations(conn)
            app.state.migrations_ready = migrations_at_head(conn)
        finally:
            conn.close()
        log.info("migrations_applied", extra={"newly_applied": applied})

        # Register the configured model in the registry (Block 6) so it appears in
        # the dashboard, then build + load it unless a backend was injected.
        if settings.model_path is not None:
            model_service.register_configured(settings.model_path, settings.model_id)
        if not injected_backend and settings.model_path is not None:
            try:
                built = build_backend(settings)
                await built.load()
                app.state.backend = built
                log.info("model_loaded", extra={"model_id": settings.model_id})
            except BackendError as exc:
                app.state.backend = None
                log.warning("model_load_failed", extra={"error": str(exc)})

        # Start the metrics sampler once the loop is running (skip if disabled).
        if settings.metrics_interval_s > 0:
            telemetry.start(lambda: getattr(app.state, "backend", None))

        try:
            yield
        finally:
            # Graceful drain (Block 9): stop admitting new work and let bounded
            # in-flight generations finish (or time out) before releasing the model,
            # so a rolling restart never severs active requests.
            scheduler.begin_drain()
            drained = await scheduler.wait_drained(settings.drain_timeout_s)
            log.info("drain_complete", extra={"drained": drained})
            await telemetry.stop()
            await model_service.shutdown()
            logging.getLogger().removeHandler(log_collector)
            active: InferenceBackend | None = getattr(app.state, "backend", None)
            if active is not None and not injected_backend:
                await active.unload()
            log.info("shutdown")

    app = FastAPI(
        title="Inference Engine",
        version=__version__,
        summary=(
            "Local GGUF inference: OpenAI + Anthropic APIs, dashboard, "
            "model lifecycle (Blocks 0–8)."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.backend = backend
    app.state.gateway = Gateway(
        settings,
        KeyStore(settings.db_path),  # type: ignore[arg-type]
        UsageStore(settings.db_path),  # type: ignore[arg-type]
    )
    app.state.event_bus = event_bus
    app.state.counters = counters
    app.state.telemetry = telemetry
    app.state.scheduler = scheduler
    app.state.audit = AuditLog(settings.db_path)  # type: ignore[arg-type]

    # Trusted-network policy (Block 9b): parse the IP allowlist once.
    allow_networks = [ipaddress.ip_network(c, strict=False) for c in settings.ip_allowlist]
    app.state.log_collector = log_collector
    app.state.model_registry = model_registry
    app.state.model_service = model_service

    @app.middleware("http")
    async def _assign_request_id(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        # A correlation id available to error handlers even for requests that fail
        # validation before the endpoint runs. The OpenAI edge overrides this with
        # its ``chatcmpl-`` id once it starts serving.
        incoming = request.headers.get("x-request-id")
        request.state.request_id = incoming or f"req-{uuid.uuid4().hex}"
        return await call_next(request)

    @app.middleware("http")
    async def _limit_body_size(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        max_bytes = settings.max_request_bytes
        if max_bytes > 0:
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                return error_response(
                    "request body too large",
                    status_code=413,
                    type="invalid_request_error",
                    code="payload_too_large",
                )
        return await call_next(request)

    @app.middleware("http")
    async def _ip_allowlist(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        # Trusted-network policy (Block 9b): when an allowlist is configured, only
        # permitted client IPs may reach any endpoint. Registered last so it is the
        # outermost middleware and rejects before any other work happens.
        if allow_networks:
            client_ip = _client_ip(request, trust_forwarded_for=settings.trust_forwarded_for)
            if client_ip is None or not _ip_permitted(client_ip, allow_networks):
                return error_response(
                    "client address not permitted",
                    status_code=403,
                    type="invalid_request_error",
                    code="ip_not_allowed",
                )
        return await call_next(request)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(openai_router)
    app.include_router(anthropic_router)
    app.include_router(ops_router)
    app.include_router(ws_router)
    app.include_router(admin_router)
    app.include_router(models_router)
    _mount_dashboard(app)
    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the built operator dashboard SPA from ``engine/static`` if present.

    The SPA is a build artifact (not committed); when it has not been built the
    engine still runs headless and the API is unaffected. Access control lives on
    the data endpoints (all operator surfaces are gated), so the static shell is
    served without relying on route obscurity: it is useless without a valid key
    when the server is network-bound.
    """
    static_dir = Path(__file__).parent / "static"
    if not (static_dir / "index.html").is_file():
        return
    app.mount("/dashboard", StaticFiles(directory=static_dir, html=True), name="dashboard")

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard/")
