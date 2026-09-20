"""Operator REST surface for operability data.

- ``GET /metrics``     — a current metrics snapshot (the WebSocket's REST fallback).
- ``GET /logs``        — recent structured ``log_events`` (filterable), for the Logs view.
- ``GET /diagnostics`` — the redacted diagnostics bundle for support.

All three are gated to authenticated operator use or loopback development use, the
same gate as the WebSocket streams. None of them can return prompts, responses, or
secrets: metrics are aggregate counters, logs hold only structured metadata, and
the diagnostics config is passed through a redactor.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from engine.api.deps import require_operator
from engine.telemetry import diagnostics
from engine.telemetry.logbuffer import LogCollector, read_log_events
from engine.telemetry.service import Telemetry

router = APIRouter(tags=["operability"])


@router.get("/metrics")
def metrics(request: Request) -> dict[str, Any]:
    require_operator(request)
    telemetry: Telemetry = request.app.state.telemetry
    backend = getattr(request.app.state, "backend", None)
    return telemetry.build_snapshot(backend)


@router.get("/logs")
def logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    level: str | None = Query(default=None),
    request_id: str | None = Query(default=None),
) -> JSONResponse:
    require_operator(request)
    settings = request.app.state.settings
    rows = read_log_events(settings.db_path, limit=limit, level=level, request_id=request_id)
    return JSONResponse(content={"events": rows})


@router.get("/diagnostics")
def diagnostics_bundle(request: Request) -> JSONResponse:
    require_operator(request)
    telemetry: Telemetry = request.app.state.telemetry
    collector: LogCollector | None = getattr(request.app.state, "log_collector", None)
    backend = getattr(request.app.state, "backend", None)
    bundle = diagnostics.build_bundle(
        settings=request.app.state.settings,
        backend=backend,
        collector=collector,
        snapshot=telemetry.build_snapshot(backend),
    )
    return JSONResponse(content=bundle)
