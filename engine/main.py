"""FastAPI application factory and lifecycle.

Block 0 wires together configuration, structured logging, SQLite/WAL migrations,
and health endpoints. No inference, auth, dashboard, or remote surfaces are
mounted here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from engine import __version__
from engine.api import health
from engine.config import Settings, load_config
from engine.logging_setup import configure_logging, get_logger
from engine.store.db import connect
from engine.store.migrations import apply_migrations

# The active settings for the running app. Set by ``create_app`` so request
# handlers (e.g. readiness) can access configuration without a DI framework.
_active_settings: Settings | None = None


def get_settings() -> Settings:
    if _active_settings is None:
        raise RuntimeError("Application settings are not initialized")
    return _active_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    If ``settings`` is not supplied it is loaded from the standard layered
    sources. Logging is configured and database migrations are applied on
    startup via the lifespan handler.
    """
    global _active_settings
    settings = settings or load_config()
    _active_settings = settings

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
        finally:
            conn.close()
        log.info("migrations_applied", extra={"newly_applied": applied})
        try:
            yield
        finally:
            log.info("shutdown")

    app = FastAPI(
        title="Inference Engine",
        version=__version__,
        summary="Block 0 foundation — configuration, persistence, health.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(health.router)
    return app
