"""FastAPI application factory and lifecycle.

Wires configuration, structured logging, SQLite/WAL migrations, health endpoints
(Block 0), and the OpenAI-compatible API edge over the internal inference backend
(Blocks 1–2). The API edge never calls llama.cpp directly — it translates onto the
internal ``GenerationRequest``/token-stream contract.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from engine import __version__
from engine.api import health
from engine.api.errors import error_response, register_exception_handlers
from engine.api.openai_router import router as openai_router
from engine.api.ops_router import router as ops_router
from engine.api.ws_router import router as ws_router
from engine.auth.keys import KeyStore
from engine.config import Settings, load_config
from engine.gateway import Gateway
from engine.inference.base import InferenceBackend
from engine.inference.factory import build_backend
from engine.inference.types import BackendError
from engine.logging_setup import configure_logging, get_logger
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

        # Build + load the configured model unless a backend was injected.
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
            await telemetry.stop()
            logging.getLogger().removeHandler(log_collector)
            active: InferenceBackend | None = getattr(app.state, "backend", None)
            if active is not None and not injected_backend:
                await active.unload()
            log.info("shutdown")

    app = FastAPI(
        title="Inference Engine",
        version=__version__,
        summary="Local GGUF inference with an OpenAI-compatible API + operability (Blocks 0–4).",
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
    app.state.log_collector = log_collector

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

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(openai_router)
    app.include_router(ops_router)
    app.include_router(ws_router)
    return app
