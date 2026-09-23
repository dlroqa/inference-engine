"""OpenAI-compatible HTTP endpoints.

Translates the OpenAI chat-completions dialect onto the internal
``GenerationRequest``/token-stream contract at the edge, and formats the internal
stream back into OpenAI shapes. The shared request lifecycle (auth, admission,
cleanup) lives in :mod:`engine.api.serving`; this module only does OpenAI-specific
translation. Supported subset only; see ``docs/compatibility.md``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from engine.api import serving
from engine.api.errors import OpenAIError
from engine.api.schemas.openai import (
    ChatCompletion,
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionRequest,
    Delta,
    Model,
    ModelList,
    ResponseMessage,
    Usage,
)
from engine.api.serving import Served
from engine.inference.base import InferenceBackend
from engine.inference.scheduler import SchedulerSaturated
from engine.inference.types import (
    FinishReason,
    GenerationRequest,
    Message,
)

router = APIRouter(prefix="/v1", tags=["openai"])

# Emit at most one live-feed progress event per this interval, per request, so the
# feed stays bounded even for long or highly concurrent generations.
_FEED_PROGRESS_INTERVAL_S = 0.25

_FINISH = {FinishReason.STOP: "stop", FinishReason.LENGTH: "length"}


def _finish_reason(reason: FinishReason) -> str:
    return _FINISH.get(reason, "stop")


def _ready_backend(request: Request) -> InferenceBackend:
    # The pool is homogeneous (one model_id); any ready backend gives capabilities.
    registry = request.app.state.backend_registry
    backend = registry.representative()
    if backend is None:
        raise OpenAIError(
            "no model is loaded",
            status_code=503,
            type="service_unavailable",
            code="model_not_loaded",
        )
    return backend


def _saturated(exc: SchedulerSaturated) -> OpenAIError:
    return OpenAIError(
        "the engine is at capacity; please retry shortly",
        status_code=429,
        type="rate_limit_error",
        code="engine_saturated",
        headers={"retry-after": str(exc.retry_after_s)},
    )


@router.get("/models")
def list_models(request: Request) -> ModelList:
    names = request.app.state.router.model_names()
    return ModelList(data=[Model(id=name) for name in names])


@router.post("/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest) -> Response:
    backend = _ready_backend(request)
    caps = backend.capabilities()
    router = request.app.state.router
    model_id = body.model
    if not router.known_model(model_id):
        raise OpenAIError(
            f"model {model_id!r} not found; this engine serves {router.model_names()}",
            status_code=404,
            type="invalid_request_error",
            param="model",
            code="model_not_found",
        )

    # Structured output (Block 8): honored via constrained decoding when the backend
    # supports it; otherwise rejected clearly (never silently unconstrained text).
    mode, schema = body.structured_output()
    if mode != "none" and not request.app.state.settings.allow_structured_output:
        raise OpenAIError(
            "structured output is disabled by the operator",
            status_code=400,
            type="invalid_request_error",
            param="response_format",
            code="structured_output_disabled",
        )
    if mode != "none" and not caps.supports_structured_output:
        raise OpenAIError(
            "structured output (response_format json) is not supported by the loaded backend",
            status_code=400,
            type="invalid_request_error",
            param="response_format",
            code="structured_output_unsupported",
        )
    if mode == "json_schema" and not schema:
        raise OpenAIError(
            "response_format json_schema requires a non-empty 'schema'",
            status_code=400,
            type="invalid_request_error",
            param="response_format",
        )

    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    request.state.request_id = request_id
    endpoint = "/v1/chat/completions"
    prompt_text = "\n".join(m.content for m in body.messages)

    gen_request = GenerationRequest(
        messages=[Message(role=m.role, content=m.content) for m in body.messages],
        request_id=request_id,
        max_tokens=body.effective_max_tokens(),
        temperature=body.temperature,
        top_p=body.top_p,
        presence_penalty=body.presence_penalty,
        frequency_penalty=body.frequency_penalty,
        stop=body.stop_list(),
        seed=body.seed,
        json_object=mode == "json_object",
        json_schema=schema if mode == "json_schema" else None,
    )

    served = await serving.start_generation(
        request,
        gen_request,
        endpoint=endpoint,
        model_id=model_id,
        route_model=model_id,
        prompt_text=prompt_text,
        max_tokens=body.effective_max_tokens() or 256,
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

    completion = ChatCompletion(
        id=request_id,
        model=model_id,
        choices=[
            ChatCompletionChoice(
                message=ResponseMessage(content=result.text),
                finish_reason=_finish_reason(result.finish_reason),
            )
        ],
        usage=Usage(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.prompt_tokens + result.completion_tokens,
        ),
    )
    return JSONResponse(content=completion.model_dump(), headers=base_headers)


async def _sse(served: Served, model_id: str) -> AsyncIterator[str]:
    request_id = served.request_id
    telemetry = served.telemetry
    stream = served.stream

    def chunk(delta: Delta, finish: str | None = None) -> str:
        payload = ChatCompletionChunk(
            id=request_id,
            model=model_id,
            choices=[ChatCompletionChunkChoice(delta=delta, finish_reason=finish)],
        )
        return f"data: {payload.model_dump_json()}\n\n"

    emitted = 0
    last_progress = time.monotonic()
    error: BaseException | None = None
    try:
        # Opening chunk carries the assistant role, per OpenAI framing.
        yield chunk(Delta(role="assistant"))
        async for token in stream:
            emitted += 1
            yield chunk(Delta(content=token.text))
            now = time.monotonic()
            if now - last_progress >= _FEED_PROGRESS_INTERVAL_S:
                telemetry.request_progress(request_id=request_id, tokens=emitted)
                last_progress = now
        result = stream.result
        finish = _finish_reason(result.finish_reason) if result else "stop"
        yield chunk(Delta(), finish=finish)
        yield "data: [DONE]\n\n"
    except Exception as exc:  # mid-stream failure (headers already sent)
        # GeneratorExit/CancelledError (client disconnect) are BaseException, not
        # Exception, so they propagate to ``finally`` without a spurious error frame.
        error = exc
        yield (
            'data: {"error": {"message": "generation failed", '
            '"type": "server_error", "code": null}}\n\n'
        )
        yield "data: [DONE]\n\n"
    finally:
        # The aclose await can be interrupted by a client disconnect, so the
        # synchronous accounting cleanup runs in an inner ``finally`` that always
        # executes (see engine.api.serving.finish_generation).
        try:
            await stream.aclose()
        finally:
            serving.finish_generation(served, error=error)
