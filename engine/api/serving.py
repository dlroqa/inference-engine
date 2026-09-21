"""Shared request-serving lifecycle for the provider API edges.

Both the OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`) edges
translate their dialect onto the internal ``GenerationRequest`` and then run the
*same* lifecycle: authenticate + rate/quota (Block 3), admission control
(Block 7), start a generation, and — on completion, failure, or client
disconnect — release the concurrency slot, decrement the active gauge, and record
usage exactly once.

That cleanup is the bug-prone part (a disconnect can interrupt the ``aclose``
await), so it lives here once, behind :func:`finish_generation`, which the callers
invoke from an inner ``finally`` that contains no awaits. Provider-specific
request/response translation stays in the routers; nothing provider-specific
leaks into this module or the inference worker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from engine.gateway import ApiAccess, Gateway
from engine.inference.base import GenerationStream, InferenceBackend
from engine.inference.scheduler import Scheduler, SchedulerLease, SchedulerSaturated
from engine.inference.types import FinishReason, GenerationRequest, GenerationResult
from engine.logging_setup import get_logger
from engine.telemetry.counters import Counters
from engine.telemetry.service import Telemetry
from engine.telemetry.taxonomy import classify

_log = get_logger("engine.request")


@dataclass(slots=True)
class Served:
    """Handle for one in-flight generation and the state needed to finish it."""

    access: ApiAccess
    lease: SchedulerLease
    stream: GenerationStream
    request_id: str
    model_id: str
    endpoint: str
    counters: Counters
    telemetry: Telemetry
    scheduler: Scheduler


async def start_generation(
    request: Request,
    gen_request: GenerationRequest,
    backend: InferenceBackend,
    *,
    endpoint: str,
    model_id: str,
    prompt_text: str,
    max_tokens: int,
    request_id: str,
    on_saturated: Callable[[SchedulerSaturated], Exception],
) -> Served:
    """Authenticate, admit, and start a generation; return a :class:`Served`.

    Raises the dialect's own error type on auth/limit/quota rejection (via
    ``gateway.authorize``) and on saturation (via ``on_saturated``); on either it
    leaves no counters or slots held.
    """
    gateway: Gateway = request.app.state.gateway
    telemetry: Telemetry = request.app.state.telemetry
    counters: Counters = request.app.state.counters
    scheduler: Scheduler = request.app.state.scheduler

    access = gateway.authorize(
        request, prompt_text=prompt_text, max_tokens=max_tokens, endpoint=endpoint
    )

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
        raise on_saturated(exc) from exc

    counters.request_started()
    telemetry.request_start(
        request_id=request_id, endpoint=endpoint, model=model_id, key_id=access.key_id
    )

    try:
        stream = backend.generate(gen_request)
    except BaseException as exc:
        lease.release()
        access.abort()
        counters.request_finished(prompt_tokens=0, completion_tokens=0, error=True)
        telemetry.request_error(
            request_id=request_id, category=classify(exc).value, endpoint=endpoint, model=model_id
        )
        raise

    return Served(
        access=access,
        lease=lease,
        stream=stream,
        request_id=request_id,
        model_id=model_id,
        endpoint=endpoint,
        counters=counters,
        telemetry=telemetry,
        scheduler=scheduler,
    )


def finish_generation(served: Served, *, error: BaseException | None) -> GenerationResult | None:
    """Release capacity and record usage for a finished/disconnected generation.

    Synchronous and idempotent-safe: call it exactly once per :class:`Served`, from
    an inner ``finally`` so it runs even if an ``aclose`` await was interrupted by a
    client disconnect. Returns the terminal result (``None`` if the generation was
    cut off before one was produced).
    """
    result = served.stream.result
    # A disconnect leaves ``error`` unset with either a CANCELLED finish or — if the
    # aclose await was itself interrupted — no terminal result at all.
    cancelled = error is None and (result is None or result.finish_reason == FinishReason.CANCELLED)
    if served.stream.backpressure_waits:
        served.scheduler.note_slow_consumer(served.stream.backpressure_waits)
    served.lease.release(cancelled=cancelled)

    prompt_tokens = result.prompt_tokens if result else 0
    completion_tokens = result.completion_tokens if result else 0
    served.access.finalize(
        request_id=served.request_id,
        model=served.model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        status=500 if error is not None else 200,
    )
    served.counters.request_finished(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        error=error is not None,
    )
    if error is not None:
        category = classify(error)
        served.telemetry.request_error(
            request_id=served.request_id,
            category=category.value,
            endpoint=served.endpoint,
            model=served.model_id,
        )
        _log.error(
            "request_error",
            extra={
                "request_id": served.request_id,
                "route": served.endpoint,
                "model": served.model_id,
                "category": category.value,
                "stage": "generation",
                "detail": "generation failed mid-stream",
            },
            exc_info=error,
        )
    else:
        served.telemetry.request_end(
            request_id=served.request_id,
            model=served.model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=result.finish_reason.value if result else "stop",
            ttft_ms=result.timings.ttft_ms if result else None,
            total_ms=result.timings.total_ms if result else None,
        )
    return result
