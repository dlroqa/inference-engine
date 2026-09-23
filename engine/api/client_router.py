"""Client-scoped account contract (Block 11.4).

A small, read-only self-service surface a **client** consumes with their own API
key: who they are, their plan, their usage, and the services they may call.

The single most important property here is **authorization isolation**: the
client is always resolved from the authenticated key, never from any request
field, so one client can never observe another's data. The :func:`client_context`
dependency is that boundary — every endpoint depends on it.

Unlike the operator surface (`/admin/*`, which allows loopback dev access), these
endpoints always require a real, client-owned API key.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from engine.api.errors import OpenAIError
from engine.billing.client_events import ClientEvent, ClientEventLog, ClientEventNotifier
from engine.billing.store import STATUS_ACTIVE, STATUS_REVOKED, BillingStore
from engine.gateway import Gateway, extract_token
from engine.quota.store import WindowUsage
from engine.quota.windows import week_start

router = APIRouter(prefix="/client", tags=["client"])


@dataclass(slots=True)
class ClientContext:
    """The authenticated client's identity, derived solely from their API key."""

    client_id: str
    key_id: str


def _stream_token(request: Request) -> str | None:
    """Token for SSE: query param (browsers can't set headers on EventSource) or
    the usual Authorization/x-api-key header."""
    token = request.query_params.get("api_key") or request.query_params.get("token")
    if token:
        return token.strip()
    return extract_token(request)


def client_context(request: Request) -> ClientContext:
    """Authenticate a client by API key (header) and resolve their client id."""
    return _resolve_client(request, extract_token(request))


def client_context_stream(request: Request) -> ClientContext:
    """Like :func:`client_context` but also accepts the key as a query param, for
    SSE where the browser cannot set an Authorization header."""
    return _resolve_client(request, _stream_token(request))


def _resolve_client(request: Request, token: str | None) -> ClientContext:
    """The isolation boundary: the client id comes from the key, never from the
    request, so a caller can only ever act as themselves."""
    gateway: Gateway = request.app.state.gateway
    billing: BillingStore = request.app.state.billing
    if not token:
        raise OpenAIError(
            "missing API key",
            status_code=401,
            type="invalid_request_error",
            code="missing_api_key",
        )
    record = gateway.keys.verify(token)  # rejects unknown/revoked_at keys
    if record is None:
        raise OpenAIError(
            "invalid API key",
            status_code=401,
            type="invalid_request_error",
            code="invalid_api_key",
        )
    access = billing.key_access(record.id)
    if access.status == STATUS_REVOKED:
        raise OpenAIError(
            "API key revoked",
            status_code=403,
            type="invalid_request_error",
            code="key_revoked",
        )
    if access.status != STATUS_ACTIVE:
        raise OpenAIError(
            "API key suspended for this account",
            status_code=403,
            type="invalid_request_error",
            code="key_suspended",
        )
    client_id = billing.client_id_for_key(record.id)
    if client_id is None:
        # A valid operator/unowned key is not a client credential.
        raise OpenAIError(
            "this endpoint requires a client-owned API key",
            status_code=403,
            type="invalid_request_error",
            code="not_a_client_key",
        )
    return ClientContext(client_id=client_id, key_id=record.id)


# --------------------------------------------------------------------------
# Response models (typed so the published OpenAPI is precise)
# --------------------------------------------------------------------------


class PlanSummary(BaseModel):
    id: str
    name: str
    quota_5h_cu: float
    quota_weekly_cu: float
    rate_limit_per_min: int | None
    allowed_models: list[str] | None  # null == all models


class ClientMe(BaseModel):
    id: str
    email: str | None
    status: str
    plan: PlanSummary | None


class PlanInfo(BaseModel):
    plan: PlanSummary | None
    entitled: bool  # whether a plan entitlement currently applies


class WindowInfo(BaseModel):
    used_cu: float
    limit_cu: float  # 0 == unlimited
    remaining_cu: float | None  # null == unlimited
    reset_at: float


class UsageInfo(BaseModel):
    key_id: str
    cu_5h: WindowInfo
    cu_weekly: WindowInfo
    client_weekly_cu: float  # aggregate across all of the client's keys


class ServicesInfo(BaseModel):
    models: list[str]
    structured_output: bool


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


def _plan_summary(billing: BillingStore, plan_id: str | None) -> PlanSummary | None:
    if plan_id is None:
        return None
    plan = billing.get_plan(plan_id)
    if plan is None:
        return None
    return PlanSummary(
        id=plan.id,
        name=plan.name,
        quota_5h_cu=plan.quota_5h_cu,
        quota_weekly_cu=plan.quota_weekly_cu,
        rate_limit_per_min=plan.rate_limit_per_min,
        allowed_models=list(plan.allowed_models) if plan.allowed_models is not None else None,
    )


def _window(w: WindowUsage) -> WindowInfo:
    remaining = None if w.limit <= 0 else w.remaining
    return WindowInfo(used_cu=w.used, limit_cu=w.limit, remaining_cu=remaining, reset_at=w.reset_at)


@router.get("/me")
def get_me(request: Request) -> ClientMe:
    ctx = client_context(request)
    billing: BillingStore = request.app.state.billing
    client = billing.get_client(ctx.client_id)
    if client is None:  # pragma: no cover - key resolved to it a moment ago
        raise OpenAIError("client not found", status_code=404, code="client_not_found")
    entitlement = billing.key_access(ctx.key_id).entitlement
    plan = _plan_summary(billing, entitlement.plan_id if entitlement else None)
    return ClientMe(id=client.id, email=client.email, status=client.status, plan=plan)


@router.get("/plan")
def get_plan(request: Request) -> PlanInfo:
    ctx = client_context(request)
    billing: BillingStore = request.app.state.billing
    entitlement = billing.key_access(ctx.key_id).entitlement
    plan = _plan_summary(billing, entitlement.plan_id if entitlement else None)
    return PlanInfo(plan=plan, entitled=entitlement is not None)


@router.get("/usage")
def get_usage(request: Request) -> UsageInfo:
    ctx = client_context(request)
    gateway: Gateway = request.app.state.gateway
    billing: BillingStore = request.app.state.billing
    settings = request.app.state.settings
    entitlement = billing.key_access(ctx.key_id).entitlement
    limit_5h = entitlement.quota_5h_cu if entitlement else settings.quota_5h_cu
    limit_week = entitlement.quota_weekly_cu if entitlement else settings.quota_weekly_cu
    now = time.time()
    u5h = gateway.usage.usage_5h(ctx.key_id, limit_5h, now)
    uweek = gateway.usage.usage_weekly(ctx.key_id, limit_week, now)
    return UsageInfo(
        key_id=ctx.key_id,
        cu_5h=_window(u5h),
        cu_weekly=_window(uweek),
        client_weekly_cu=billing.client_usage_cu(ctx.client_id, week_start(now)),
    )


@router.get("/services")
def get_services(request: Request) -> ServicesInfo:
    ctx = client_context(request)
    billing: BillingStore = request.app.state.billing
    settings = request.app.state.settings
    names: list[str] = list(request.app.state.router.model_names())
    entitlement = billing.key_access(ctx.key_id).entitlement
    if entitlement is not None and entitlement.allowed_models is not None:
        allowed = set(entitlement.allowed_models)
        names = [n for n in names if n in allowed]
    return ServicesInfo(models=names, structured_output=settings.allow_structured_output)


# --------------------------------------------------------------------------
# Client event stream (SSE) + reconciliation / polling fallback (Block 11.5)
# --------------------------------------------------------------------------


class ClientEventOut(BaseModel):
    id: int
    event_id: str
    type: str
    ts: float
    data: dict  # the event envelope's ``data`` object


class ClientEventsPage(BaseModel):
    events: list[ClientEventOut]
    last_id: int


def _require_client_events(request: Request) -> ClientEventLog:
    if not request.app.state.settings.client_events_enabled:
        raise OpenAIError(
            "client event streaming is not enabled",
            status_code=404,
            type="invalid_request_error",
            code="client_events_disabled",
        )
    return request.app.state.client_event_log


def _cursor(request: Request) -> int:
    """The resume point: the ``Last-Event-ID`` header or a ``since`` query param."""
    raw = request.headers.get("last-event-id") or request.query_params.get("since")
    try:
        return max(0, int(raw)) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _event_out(event: object) -> ClientEventOut:
    envelope = json.loads(event.payload)  # type: ignore[attr-defined]
    return ClientEventOut(
        id=event.id,  # type: ignore[attr-defined]
        event_id=event.event_id,  # type: ignore[attr-defined]
        type=event.type,  # type: ignore[attr-defined]
        ts=event.ts,  # type: ignore[attr-defined]
        data=envelope.get("data", {}),
    )


@router.get("/events")
def get_events(request: Request, since: int = 0, limit: int = 100) -> ClientEventsPage:
    """Reconciliation / polling fallback: events with ``id > since`` for the caller.

    Same data as the SSE stream, for clients that cannot hold a connection or need
    to catch up after a gap.
    """
    ctx = client_context(request)
    log = _require_client_events(request)
    events = log.list_since(ctx.client_id, max(0, since), limit=limit)
    out = [_event_out(e) for e in events]
    last_id = out[-1].id if out else max(0, since)
    return ClientEventsPage(events=out, last_id=last_id)


@router.get("/events/stream")
async def stream_events(request: Request) -> StreamingResponse:
    """SSE stream of the caller's account events, resumable via ``Last-Event-ID``.

    On connect, missed events (``id > cursor``) are replayed, then live events are
    pushed as they occur. Each frame carries ``id: <seq>`` so the browser tracks
    the cursor automatically; periodic heartbeats keep the connection open.
    """
    ctx = client_context_stream(request)
    log = _require_client_events(request)
    notifier: ClientEventNotifier = request.app.state.client_event_notifier
    heartbeat = request.app.state.settings.client_sse_heartbeat_s
    gen = sse_event_stream(
        log=log,
        notifier=notifier,
        client_id=ctx.client_id,
        cursor=_cursor(request),
        heartbeat_s=heartbeat,
        is_disconnected=request.is_disconnected,
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


async def sse_event_stream(
    *,
    log: ClientEventLog,
    notifier: ClientEventNotifier,
    client_id: str,
    cursor: int,
    heartbeat_s: float,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[bytes]:
    """The SSE byte stream: replay from ``cursor`` then live events + heartbeats.

    ``is_disconnected`` is injected (the request's own check in production, a stub
    in tests) so this loop -- the real streaming logic -- is testable without a
    live server, which cannot reliably signal disconnect through the test client.
    """
    yield b": connected\n\n"
    while True:
        # Capture the wakeup *before* reading, so a notify during the read is not
        # lost, then drain everything newer than the cursor.
        waiter = notifier.event(client_id)
        for event in log.list_since(client_id, cursor, limit=200):
            yield _sse_frame(event)
            cursor = event.id
        if await is_disconnected():
            return
        try:
            await asyncio.wait_for(waiter.wait(), timeout=heartbeat_s)
        except TimeoutError:
            yield b": keep-alive\n\n"


def _sse_frame(event: ClientEvent) -> bytes:
    # ``payload`` is already the compact JSON envelope; send it verbatim as data.
    text = f"id: {event.id}\nevent: {event.type}\ndata: {event.payload}\n\n"
    return text.encode("utf-8")
