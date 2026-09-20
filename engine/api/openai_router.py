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

    # generate() may raise BackendBusy/NotReady synchronously — surfaced as a
    # proper HTTP error before any streaming response begins.
    stream = backend.generate(gen_request)

    if body.stream:
        return StreamingResponse(
            _sse(stream, request_id, model_id),
            media_type="text/event-stream",
            headers={"x-request-id": request_id, "cache-control": "no-cache"},
        )

    result = await stream.collect()
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
    return JSONResponse(content=completion.model_dump(), headers={"x-request-id": request_id})


async def _sse(stream: GenerationStream, request_id: str, model_id: str) -> AsyncIterator[str]:
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
