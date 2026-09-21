"""Anthropic-compatible Messages endpoint (``POST /v1/messages``).

Translates the Anthropic Messages dialect onto the internal
``GenerationRequest``/token-stream contract at the edge, and emits the Anthropic
streaming event sequence. The shared request lifecycle (auth, admission, cleanup)
lives in :mod:`engine.api.serving`; no Anthropic types leak into the worker.
Supported subset only (text content, core sampling); see ``docs/compatibility.md``.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from engine.api import serving
from engine.api.errors import AnthropicError
from engine.api.schemas.anthropic import (
    AnthropicUsage,
    MessagesRequest,
    MessagesResponse,
    TextBlock,
)
from engine.api.serving import Served
from engine.inference.base import InferenceBackend
from engine.inference.scheduler import SchedulerSaturated
from engine.inference.types import BackendState, FinishReason, GenerationRequest

router = APIRouter(prefix="/v1", tags=["anthropic"])

_FEED_PROGRESS_INTERVAL_S = 0.25

_STOP_REASON = {FinishReason.STOP: "end_turn", FinishReason.LENGTH: "max_tokens"}


def _stop_reason(reason: FinishReason) -> str:
    return _STOP_REASON.get(reason, "end_turn")


def _ready_backend(request: Request) -> InferenceBackend:
    backend: InferenceBackend | None = getattr(request.app.state, "backend", None)
    if backend is None or backend.state not in (BackendState.READY, BackendState.GENERATING):
        raise AnthropicError("no model is loaded", status_code=503, type="api_error")
    return backend


def _saturated(exc: SchedulerSaturated) -> AnthropicError:
    # Anthropic signals capacity with 529 overloaded_error; SDK clients retry it.
    return AnthropicError(
        "the engine is at capacity; please retry shortly",
        status_code=529,
        type="overloaded_error",
        headers={"retry-after": str(exc.retry_after_s)},
    )


def _event(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


@router.post("/messages")
async def messages(request: Request, body: MessagesRequest) -> Response:
    backend = _ready_backend(request)
    model_id = backend.capabilities().model_id
    if body.model != model_id:
        raise AnthropicError(
            f"model {body.model!r} not found; this engine serves {model_id!r}",
            status_code=404,
            type="not_found_error",
        )

    request_id = f"msg_{uuid.uuid4().hex}"
    request.state.request_id = request_id
    endpoint = "/v1/messages"

    gen_request = GenerationRequest(
        messages=body.to_internal_messages(),
        request_id=request_id,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        top_p=body.top_p,
        top_k=body.top_k,
        stop=list(body.stop_sequences),
    )

    served = await serving.start_generation(
        request,
        gen_request,
        backend,
        endpoint=endpoint,
        model_id=model_id,
        prompt_text=body.prompt_text(),
        max_tokens=body.max_tokens,
        request_id=request_id,
        on_saturated=_saturated,
    )

    base_headers = {"x-request-id": request_id, **served.access.headers}

    if body.stream:
        return StreamingResponse(
            _sse(served, model_id),
            media_type="text/event-stream",
            headers={**base_headers, "cache-control": "no-cache"},
        )

    try:
        result = await served.stream.collect()
    except BaseException as exc:
        try:
            await served.stream.aclose()
        finally:
            serving.finish_generation(served, error=exc)
        raise
    serving.finish_generation(served, error=None)

    response = MessagesResponse(
        id=request_id,
        model=model_id,
        content=[TextBlock(text=result.text)],
        stop_reason=_stop_reason(result.finish_reason),
        stop_sequence=None,
        usage=AnthropicUsage(
            input_tokens=result.prompt_tokens,
            output_tokens=result.completion_tokens,
        ),
    )
    return JSONResponse(content=response.model_dump(), headers=base_headers)


async def _sse(served: Served, model_id: str) -> AsyncIterator[str]:
    stream = served.stream
    request_id = served.request_id
    telemetry = served.telemetry
    input_tokens = stream.prompt_tokens

    emitted = 0
    last_progress = time.monotonic()
    error: BaseException | None = None
    try:
        yield _event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": request_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model_id,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                },
            },
        )
        yield _event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        yield _event("ping", {"type": "ping"})
        async for token in stream:
            emitted += 1
            yield _event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": token.text},
                },
            )
            now = time.monotonic()
            if now - last_progress >= _FEED_PROGRESS_INTERVAL_S:
                telemetry.request_progress(request_id=request_id, tokens=emitted)
                last_progress = now
        result = stream.result
        yield _event("content_block_stop", {"type": "content_block_stop", "index": 0})
        stop_reason = _stop_reason(result.finish_reason) if result else "end_turn"
        output_tokens = result.completion_tokens if result else emitted
        yield _event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        )
        yield _event("message_stop", {"type": "message_stop"})
    except Exception as exc:  # mid-stream failure (headers already sent)
        error = exc
        yield _event(
            "error",
            {"type": "error", "error": {"type": "api_error", "message": "generation failed"}},
        )
    finally:
        # The aclose await can be interrupted by a client disconnect, so the
        # synchronous accounting cleanup runs in an inner ``finally`` (see
        # engine.api.serving.finish_generation).
        try:
            await stream.aclose()
        finally:
            serving.finish_generation(served, error=error)
