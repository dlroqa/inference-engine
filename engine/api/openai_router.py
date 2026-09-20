"""OpenAI-compatible HTTP endpoints.

Translates the OpenAI chat-completions dialect onto the internal
``GenerationRequest``/token-stream contract (Block 1) at the edge, and formats the
internal stream back into OpenAI shapes. Supported subset only; see
``docs/compatibility.md``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

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
from engine.gateway import ApiAccess, Gateway
from engine.inference.base import GenerationStream, InferenceBackend
from engine.inference.types import (
    BackendState,
    FinishReason,
    GenerationRequest,
    Message,
)

router = APIRouter(prefix="/v1", tags=["openai"])

_FINISH = {FinishReason.STOP: "stop", FinishReason.LENGTH: "length"}


def _finish_reason(reason: FinishReason) -> str:
    return _FINISH.get(reason, "stop")


def _ready_backend(request: Request) -> InferenceBackend:
    backend: InferenceBackend | None = getattr(request.app.state, "backend", None)
    if backend is None or backend.state not in (BackendState.READY, BackendState.GENERATING):
        raise OpenAIError(
            "no model is loaded",
            status_code=503,
            type="service_unavailable",
            code="model_not_loaded",
        )
    return backend


@router.get("/models")
def list_models(request: Request) -> ModelList:
    backend: InferenceBackend | None = getattr(request.app.state, "backend", None)
    if backend is not None and backend.state in (BackendState.READY, BackendState.GENERATING):
        caps = backend.capabilities()
        return ModelList(data=[Model(id=caps.model_id)])
    return ModelList(data=[])


@router.post("/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest) -> Response:
    backend = _ready_backend(request)
    model_id = backend.capabilities().model_id
    if body.model != model_id:
        raise OpenAIError(
            f"model {body.model!r} not found; this engine serves {model_id!r}",
            status_code=404,
            type="invalid_request_error",
            param="model",
            code="model_not_found",
        )

    # Authenticate + rate/concurrency + quota pre-check (raises OpenAIError on
    # rejection). Attribution + CU debit happen at finalize.
    gateway: Gateway = request.app.state.gateway
    prompt_text = "\n".join(m.content for m in body.messages)
    access = gateway.authorize(
        request,
        prompt_text=prompt_text,
        max_tokens=body.effective_max_tokens() or 256,
        endpoint="/v1/chat/completions",
    )

    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    gen_request = GenerationRequest(
        messages=[Message(role=m.role, content=m.content) for m in body.messages],
        request_id=request_id,
        max_tokens=body.effective_max_tokens(),
        temperature=body.temperature,
        top_p=body.top_p,
        stop=body.stop_list(),
        seed=body.seed,
    )

    try:
        # generate() may raise BackendBusy/NotReady synchronously — surfaced as a
        # proper HTTP error before any streaming response begins.
        stream = backend.generate(gen_request)
    except BaseException:
        access.abort()
        raise

    base_headers = {"x-request-id": request_id, **access.headers}

    if body.stream:
        return StreamingResponse(
            _sse(stream, request_id, model_id, access),
            media_type="text/event-stream",
            headers={**base_headers, "cache-control": "no-cache"},
        )

    try:
        result = await stream.collect()
    except BaseException:
        access.abort()
        raise
    access.finalize(
        request_id=request_id,
        model=model_id,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        status=200,
    )
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


async def _sse(
    stream: GenerationStream, request_id: str, model_id: str, access: ApiAccess
) -> AsyncIterator[str]:
    def chunk(delta: Delta, finish: str | None = None) -> str:
        payload = ChatCompletionChunk(
            id=request_id,
            model=model_id,
            choices=[ChatCompletionChunkChoice(delta=delta, finish_reason=finish)],
        )
        return f"data: {payload.model_dump_json()}\n\n"

    try:
        # Opening chunk carries the assistant role, per OpenAI framing.
        yield chunk(Delta(role="assistant"))
        async for token in stream:
            yield chunk(Delta(content=token.text))
        result = stream.result
        finish = _finish_reason(result.finish_reason) if result else "stop"
        yield chunk(Delta(), finish=finish)
        yield "data: [DONE]\n\n"
    finally:
        # Client disconnect (generator closed) or normal completion both land
        # here; aclose cancels the worker and releases it.
        await stream.aclose()
        result = stream.result
        # Record usage (possibly partial on disconnect) and free the slot.
        access.finalize(
            request_id=request_id,
            model=model_id,
            prompt_tokens=result.prompt_tokens if result else 0,
            completion_tokens=result.completion_tokens if result else 0,
            status=200,
        )
