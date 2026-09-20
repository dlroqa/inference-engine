"""Operator WebSocket streams: ``/ws/metrics`` and ``/ws/feed``.

- ``/ws/metrics`` pushes periodic metrics snapshots (the sampler's output). A
  current snapshot is sent immediately on connect so the client is never blank.
- ``/ws/feed`` streams the inference live feed (request start/progress/end/error),
  replaying the recent-event ring buffer first so a late joiner has context.

Both are gated to authenticated operator use or loopback development use (see
:meth:`Gateway.operator_allowed`). Access is checked *before* accepting the
socket; unauthorized clients are closed with policy-violation code 1008.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from engine.gateway import Gateway
from engine.telemetry.events import Event, EventBus, EventChannel
from engine.telemetry.service import Telemetry

router = APIRouter(tags=["operability"])

WS_UNAUTHORIZED = 1008  # policy violation


def _ws_token(websocket: WebSocket) -> str | None:
    """Extract an API key from the handshake (query param or header)."""
    token = websocket.query_params.get("api_key") or websocket.query_params.get("token")
    if token:
        return token.strip()
    auth = websocket.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[len("bearer ") :].strip()
    x_api_key = websocket.headers.get("x-api-key")
    if x_api_key:
        return x_api_key.strip()
    return None


def _authorized(websocket: WebSocket) -> bool:
    gateway: Gateway = websocket.app.state.gateway
    host = websocket.client.host if websocket.client else None
    return gateway.operator_allowed(client_host=host, token=_ws_token(websocket))


async def _pump(websocket: WebSocket, source: AsyncIterator[Event]) -> None:
    """Forward events to the socket while watching for client disconnect."""
    aiter = source.__aiter__()
    next_event: asyncio.Task[Event] = asyncio.ensure_future(aiter.__anext__())
    next_recv: asyncio.Task[object] = asyncio.ensure_future(websocket.receive())
    try:
        while True:
            done, _ = await asyncio.wait(
                {next_event, next_recv}, return_when=asyncio.FIRST_COMPLETED
            )
            if next_recv in done:
                try:
                    message = next_recv.result()
                except WebSocketDisconnect:
                    break
                if isinstance(message, dict) and message.get("type") == "websocket.disconnect":
                    break
                # Ignore any client->server payloads; re-arm the receive.
                next_recv = asyncio.ensure_future(websocket.receive())
            if next_event in done:
                try:
                    event = next_event.result()
                except StopAsyncIteration:  # pragma: no cover - source is unbounded
                    break
                try:
                    await websocket.send_json(event.to_dict())
                except (WebSocketDisconnect, RuntimeError):
                    break
                next_event = asyncio.ensure_future(aiter.__anext__())
    finally:
        for task in (next_event, next_recv):
            task.cancel()
        await asyncio.gather(next_event, next_recv, return_exceptions=True)
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            await aclose()


@router.websocket("/ws/metrics")
async def ws_metrics(websocket: WebSocket) -> None:
    if not _authorized(websocket):
        await websocket.close(code=WS_UNAUTHORIZED)
        return
    await websocket.accept()
    telemetry: Telemetry = websocket.app.state.telemetry
    bus: EventBus = websocket.app.state.event_bus
    backend = getattr(websocket.app.state, "backend", None)
    # Immediate snapshot so the client renders without waiting a full tick.
    try:
        await websocket.send_json(telemetry.build_snapshot(backend))
    except (WebSocketDisconnect, RuntimeError):
        return
    await _pump(websocket, bus.subscribe(EventChannel.METRICS, replay_history=False))


@router.websocket("/ws/feed")
async def ws_feed(websocket: WebSocket) -> None:
    if not _authorized(websocket):
        await websocket.close(code=WS_UNAUTHORIZED)
        return
    await websocket.accept()
    bus: EventBus = websocket.app.state.event_bus
    await _pump(websocket, bus.subscribe(EventChannel.FEED, replay_history=True))
