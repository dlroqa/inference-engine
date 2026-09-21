"""OpenAI-compatible HTTP endpoints.

Translates the OpenAI chat-completions dialect onto the internal
``GenerationRequest``/token-stream contract (Block 1) at the edge, and formats the
internal stream back into OpenAI shapes. Supported subset only; see
``docs/compatibility.md``.
"""

from __future__ import annotations

import asyncio
import time
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
from engine.inference.scheduler import Scheduler, SchedulerLease, SchedulerSaturated
from engine.inference.types import (
    BackendState,
    FinishReason,
    GenerationRequest,
    Message,
)
from engine.logging_setup import get_logger
from engine.telemetry.counters import Counters
from engine.telemetry.service import Telemetry
from engine.telemetry.taxonomy import classify

router = APIRouter(prefix="/v1", tags=["openai"])
_log = get_logger("engine.request")

# Emit at most one live-feed progress event per this interval, per request, so the
# feed stays bounded even for long or highly concurrent generations.
_FEED_PROGRESS_INTERVAL_S = 0.25

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
    telemetry: Telemetry = request.app.state.telemetry
    counters: Counters = request.app.state.counters
    scheduler: Scheduler = request.app.state.scheduler
    endpoint = "/v1/chat/completions"
    prompt_text = "\n".join(m.content for m in body.messages)

    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    request.state.request_id = request_id

    access = gateway.authorize(
        request,
        prompt_text=prompt_text,
        max_tokens=body.effective_max_tokens() or 256,
        endpoint=endpoint,
    )

    # Admission control (Block 7): acquire a concurrency slot, waiting in the
    # bounded queue if the backend is busy. Saturation is an explicit, retriable
    # 429 rather than an unbounded wait or a silent memory blow-up.
    try:
        lease = await scheduler.admit()
    except SchedulerSaturated as exc:
        access.abort()
        telemetry.request_rejected(
            request_id=request_id,
            endpoint=endpoint,
            reason=exc.reason,
            retry_after_s=exc.retry_after_s,
        )
        raise OpenAIError(
            "the engine is at capacity; please retry shortly",
            status_code=429,
            type="rate_limit_error",
            code="engine_saturated",
            headers={"retry-after": str(exc.retry_after_s)},
        ) from exc

    # From here on the request is "started": it must be finished exactly once so
    # the active-request gauge and error counter stay correct.
    counters.request_started()
    telemetry.request_start(
        request_id=request_id, endpoint=endpoint, model=model_id, key_id=access.key_id
    )

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
    except BaseException as exc:
        lease.release()
        access.abort()
        counters.request_finished(prompt_tokens=0, completion_tokens=0, error=True)
        telemetry.request_error(
            request_id=request_id, category=classify(exc).value, endpoint=endpoint, model=model_id
        )
        raise

    base_headers = {"x-request-id": request_id, **access.headers}

    if body.stream:
        return StreamingResponse(
            _sse(
                stream,
                request_id,
                model_id,
                access,
                telemetry,
                counters,
                scheduler,
                lease,
                endpoint,
            ),
            media_type="text/event-stream",
            headers={**base_headers, "cache-control": "no-cache"},
        )

    try:
        result = await stream.collect()
    except BaseException as exc:
        # A client disconnect during a non-streaming call surfaces as cancellation;
        # the aclose await may itself be interrupted, so the (synchronous) capacity
        # and accounting cleanup runs in an inner ``finally`` that always executes.
        cancelled = isinstance(exc, asyncio.CancelledError)
        try:
            await stream.aclose()
        finally:
            lease.release(cancelled=cancelled)
            access.abort()
            counters.request_finished(prompt_tokens=0, completion_tokens=0, error=True)
            telemetry.request_error(
                request_id=request_id,
                category=classify(exc).value,
                endpoint=endpoint,
                model=model_id,
            )
        raise
    if stream.backpressure_waits:
        scheduler.note_slow_consumer(stream.backpressure_waits)
    lease.release()
    access.finalize(
        request_id=request_id,
        model=model_id,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        status=200,
    )
    counters.request_finished(
        prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens
    )
    telemetry.request_end(
        request_id=request_id,
        model=model_id,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        finish_reason=_finish_reason(result.finish_reason),
        ttft_ms=result.timings.ttft_ms,
        total_ms=result.timings.total_ms,
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
    stream: GenerationStream,
    request_id: str,
    model_id: str,
    access: ApiAccess,
    telemetry: Telemetry,
    counters: Counters,
    scheduler: Scheduler,
    lease: SchedulerLease,
    endpoint: str,
) -> AsyncIterator[str]:
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
        # NB: GeneratorExit/CancelledError (client disconnect) are BaseException,
        # not Exception, so they propagate to ``finally`` without a spurious error
        # frame — a disconnect is a cancellation, not a generation failure.
        error = exc
        # Surface a terminal error frame so SDK clients see a clean end-of-stream.
        yield (
            'data: {"error": {"message": "generation failed", '
            '"type": "server_error", "code": null}}\n\n'
        )
        yield "data: [DONE]\n\n"
    finally:
        # Client disconnect (generator closed) or normal completion both land
        # here; aclose cancels the worker and releases it. On a disconnect the
        # enclosing task is being cancelled, so the ``await`` below can itself be
        # interrupted — the inner ``finally`` therefore holds only synchronous
        # cleanup, guaranteeing a disconnect always frees the slot, decrements the
        # active gauge, and records usage even if the aclose await is cut short.
        try:
            await stream.aclose()
        finally:
            result = stream.result
            # Client disconnect is counted as a cancellation so capacity-freeing is
            # observable. It is distinguished from normal completion (finish STOP,
            # ``error is None``) and mid-stream failure (``error`` set): a disconnect
            # leaves ``error`` unset with either a CANCELLED finish or — if the
            # aclose await was itself interrupted — no terminal result at all. The
            # slot is released *after* aclose so the backend is READY before the next
            # queued request runs.
            cancelled = error is None and (
                result is None or result.finish_reason == FinishReason.CANCELLED
            )
            if stream.backpressure_waits:
                scheduler.note_slow_consumer(stream.backpressure_waits)
            lease.release(cancelled=cancelled)
            prompt_tokens = result.prompt_tokens if result else 0
            completion_tokens = result.completion_tokens if result else 0
            # Record usage (possibly partial on disconnect) and free the key slot.
            access.finalize(
                request_id=request_id,
                model=model_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                status=500 if error is not None else 200,
            )
            counters.request_finished(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error=error is not None,
            )
            if error is not None:
                category = classify(error)
                telemetry.request_error(
                    request_id=request_id,
                    category=category.value,
                    endpoint=endpoint,
                    model=model_id,
                )
                _log.error(
                    "request_error",
                    extra={
                        "request_id": request_id,
                        "route": endpoint,
                        "model": model_id,
                        "category": category.value,
                        "stage": "generation",
                        "detail": "generation failed mid-stream",
                    },
                    exc_info=error,
                )
            else:
                telemetry.request_end(
                    request_id=request_id,
                    model=model_id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    finish_reason=_finish_reason(result.finish_reason) if result else "stop",
                    ttft_ms=result.timings.ttft_ms if result else None,
                    total_ms=result.timings.total_ms if result else None,
                )
