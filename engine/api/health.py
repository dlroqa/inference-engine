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
from engine.store.db import connect_readonly

router = APIRouter(tags=["health"])

# Honest, documented note surfaced on readiness while no runtime exists.
INFERENCE_NOTE = "inference runtime not implemented in this build (Block 0 foundation)"


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

    body: dict[str, object] = {
        "status": "ready" if ready else "not_ready",
        "version": __version__,
        "checks": checks,
        "inference": {"available": False, "reason": INFERENCE_NOTE},
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)
