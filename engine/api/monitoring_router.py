"""Operator monitoring aggregations (Block 11.6a).

Read-only rollups the Clients / Live-Monitoring UI consumes:

- ``GET /admin/usage/attribution`` — per-key usage over a window (requests,
  tokens, CU, last-used, error count), enriched with key label + owning client.
- ``GET /admin/errors/taxonomy`` — recent error counts by taxonomy category.
- ``GET /admin/alerts`` — computed operator alerts (suspended/canceled clients,
  usage-threshold crossings, dead-lettered webhooks, unhealthy backends).

All behind :func:`require_operator`; nothing here is persisted.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Request

from engine.api.deps import require_operator
from engine.billing.client_events import ClientEventLog
from engine.billing.store import BillingStore
from engine.billing.webhooks.store import STATUS_DEAD, WebhookStore
from engine.quota.store import UsageStore
from engine.quota.windows import rolling_5h_start, week_start
from engine.telemetry.alerts import compute_alerts
from engine.telemetry.logbuffer import read_error_taxonomy

router = APIRouter(prefix="/admin", tags=["monitoring"])

_Window = Literal["5h", "week"]


def _window_start(window: str, now: float) -> float:
    return week_start(now) if window == "week" else rolling_5h_start(now)


@router.get("/usage/attribution")
def usage_attribution(request: Request, window: _Window = "5h") -> dict[str, Any]:
    require_operator(request)
    settings = request.app.state.settings
    now = time.time()
    since = _window_start(window, now)

    usage = UsageStore(settings.db_path)
    rows = usage.attribution(since)

    # Enrich with key label + owning client (a couple of small lookups; the
    # operator view is low-volume). Key/client stores are queried, not joined in
    # SQL, to keep the usage store decoupled from billing tables.
    key_store = request.app.state.gateway.keys
    billing: BillingStore = request.app.state.billing
    labels = {k.id: k.prefix for k in key_store.list()}
    label_names = {k.id: k.label for k in key_store.list()}

    out: list[dict[str, Any]] = []
    for r in rows:
        client_id = billing.client_id_for_key(r.key_id) if r.key_id else None
        out.append(
            {
                "key_id": r.key_id,
                "key_prefix": labels.get(r.key_id),
                "key_label": label_names.get(r.key_id),
                "client_id": client_id,
                "requests": r.requests,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "cu": r.cu,
                "errors": r.errors,
                "last_ts": r.last_ts,
            }
        )
    return {"window": window, "since": since, "keys": out}


@router.get("/errors/taxonomy")
def errors_taxonomy(request: Request, window: _Window = "5h") -> dict[str, Any]:
    require_operator(request)
    settings = request.app.state.settings
    now = time.time()
    since = _window_start(window, now)
    counts = read_error_taxonomy(settings.db_path, since=since)
    return {"window": window, "since": since, "categories": counts, "total": sum(counts.values())}


@router.get("/alerts")
def alerts(request: Request) -> dict[str, Any]:
    require_operator(request)
    settings = request.app.state.settings
    now = time.time()

    billing: BillingStore = request.app.state.billing
    webhooks = WebhookStore(settings.db_path)
    # The client-events table always exists; construct a reader regardless of
    # whether the SSE feature is enabled.
    events = ClientEventLog(settings.db_path)

    clients = billing.list_clients()
    dead = webhooks.count_by_status(STATUS_DEAD)
    threshold_events = events.recent_of_type(
        "usage.threshold.reached", since=now - 7 * 24 * 3600, limit=200
    )
    registry = getattr(request.app.state, "backend_registry", None)
    unhealthy = (
        [e.name for e in registry.entries if e.is_loaded() and not e.is_ready()]
        if registry is not None
        else []
    )

    result = compute_alerts(
        clients=clients,
        threshold_events=threshold_events,
        dead_delivery_count=dead,
        unhealthy_backends=unhealthy,
    )
    return {"alerts": [a.to_dict() for a in result]}
