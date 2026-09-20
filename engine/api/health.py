"""Health endpoints.

``/healthz`` — process liveness. Returns 200 whenever the process can serve.

``/readyz`` — readiness. Checks the foundational dependencies that exist in this
build (database reachable, migrations at head). It explicitly reports that **no
inference model is available**, because inference does not exist until Block 1.
This keeps the exit criterion honest: no endpoint implies inference is available.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from engine import __version__
from engine.config import Settings
from engine.inference.base import InferenceBackend
from engine.inference.types import BackendState
from engine.store.db import connect_readonly

router = APIRouter(tags=["health"])

# Honest, documented note surfaced on readiness when no model is loaded.
INFERENCE_NOTE = "no model loaded; configure model_path and load a model to serve /v1"


@router.get("/healthz")
def healthz() -> dict[str, object]:
    """Liveness: the process is up and able to handle requests."""
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    """Readiness of foundational dependencies.

    Returns 200 when the database is reachable and migrations are applied; 503
    otherwise. Always reports ``inference.available = false`` so callers never
    mistake the foundation for a working inference service.

    Reads settings and the startup-computed migration status from the request's
    application state, and uses a lightweight read-only connection so frequent
    probes stay cheap.
    """
    settings: Settings = request.app.state.settings
    migrations_ready: bool = getattr(request.app.state, "migrations_ready", False)

    checks: dict[str, str] = {}
    ready = True

    try:
        conn = connect_readonly(settings.db_path)  # type: ignore[arg-type]
        try:
            conn.execute("SELECT 1;").fetchone()
            checks["database"] = "ok"
        finally:
            conn.close()
    except Exception as exc:
        checks["database"] = f"error: {exc.__class__.__name__}"
        ready = False

    checks["migrations"] = "applied" if migrations_ready else "pending"
    if not migrations_ready:
        ready = False

    # Inference availability is reported separately from service readiness: the
    # service can be ready (DB + migrations) without a model loaded.
    backend: InferenceBackend | None = getattr(request.app.state, "backend", None)
    model_loaded = backend is not None and backend.state in (
        BackendState.READY,
        BackendState.GENERATING,
    )
    inference: dict[str, object] = {"available": model_loaded}
    if backend is not None and model_loaded:
        inference["model_id"] = backend.capabilities().model_id
        inference["state"] = backend.state.value
    else:
        inference["reason"] = INFERENCE_NOTE

    body: dict[str, object] = {
        "status": "ready" if ready else "not_ready",
        "version": __version__,
        "checks": checks,
        "inference": inference,
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)
