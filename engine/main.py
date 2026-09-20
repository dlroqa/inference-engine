"""FastAPI application factory and lifecycle.

Wires configuration, structured logging, SQLite/WAL migrations, health endpoints
(Block 0), and the OpenAI-compatible API edge over the internal inference backend
(Blocks 1–2). The API edge never calls llama.cpp directly — it translates onto the
internal ``GenerationRequest``/token-stream contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from engine import __version__
from engine.api import health
from engine.api.errors import register_exception_handlers
from engine.api.openai_router import router as openai_router
from engine.config import Settings, load_config
from engine.inference.base import InferenceBackend
from engine.inference.factory import build_backend
from engine.inference.types import BackendError
from engine.logging_setup import configure_logging, get_logger
from engine.store.db import connect
from engine.store.migrations import apply_migrations, migrations_at_head

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

        try:
            yield
        finally:
            active: InferenceBackend | None = getattr(app.state, "backend", None)
            if active is not None and not injected_backend:
                await active.unload()
            log.info("shutdown")

    app = FastAPI(
        title="Inference Engine",
        version=__version__,
        summary="Local GGUF inference with an OpenAI-compatible API (Blocks 0–2).",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.backend = backend
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(openai_router)
    return app
