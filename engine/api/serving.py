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

from engine.gateway import ApiAccess, Gateway, extract_token
from engine.inference.base import GenerationStream
from engine.inference.registry import NoBackendAvailable, RegistryLease, RouteDecision
from engine.inference.router import Router
from engine.inference.scheduler import Scheduler, SchedulerLease, SchedulerSaturated
from engine.inference.types import (
    BackendNotReadyError,
    FeatureUnsupportedError,
    FinishReason,
    GenerationRequest,
    GenerationResult,
)
from engine.logging_setup import get_logger
from engine.telemetry.counters import Counters
from engine.telemetry.route_metrics import RouteMetrics, output_tps_sample
from engine.telemetry.service import Telemetry
from engine.telemetry.taxonomy import classify

_log = get_logger("engine.request")


@dataclass(slots=True)
class Served:
    """Handle for one in-flight generation and the state needed to finish it."""

    access: ApiAccess
    lease: SchedulerLease
    registry_lease: RegistryLease
    stream: GenerationStream
    route_metrics: RouteMetrics
    request_id: str
    model_id: str
    endpoint: str
    counters: Counters
    telemetry: Telemetry
    scheduler: Scheduler


async def start_generation(
    request: Request,
    gen_request: GenerationRequest,
    *,
    endpoint: str,
    model_id: str,
    route_model: str,
    prompt_text: str,
    max_tokens: int,
    request_id: str,
    on_saturated: Callable[[SchedulerSaturated], Exception],
    required_features: frozenset[str] = frozenset(),
) -> Served:
    """HTTP entry point: extract the API token from the request, then serve.

    A thin wrapper over :func:`start_generation_core` so the gRPC edge (Block 10.6)
    can share the exact same lifecycle with a token from call metadata instead.
    """
    return await start_generation_core(
        request.app,
        token=extract_token(request),
        gen_request=gen_request,
        endpoint=endpoint,
        model_id=model_id,
        route_model=route_model,
        prompt_text=prompt_text,
        max_tokens=max_tokens,
        request_id=request_id,
        on_saturated=on_saturated,
        required_features=required_features,
    )


async def start_generation_core(
    app: object,
    *,
    token: str | None,
    gen_request: GenerationRequest,
    endpoint: str,
    model_id: str,
    route_model: str,
    prompt_text: str,
    max_tokens: int,
    request_id: str,
    on_saturated: Callable[[SchedulerSaturated], Exception],
    required_features: frozenset[str] = frozenset(),
) -> Served:
    """Authenticate, admit, route, and start a generation; return a :class:`Served`.

    Transport-agnostic: ``app`` supplies the shared state (gateway, scheduler,
    router, counters, telemetry, route-metrics) and ``token`` the API key. Raises
    the caller-mapped error on auth/limit/quota rejection and on saturation; on
    either it leaves no counters or slots held.
    """
    state = app.state  # type: ignore[attr-defined]
    gateway: Gateway = state.gateway
    telemetry: Telemetry = state.telemetry
    counters: Counters = state.counters
    scheduler: Scheduler = state.scheduler
    router: Router = state.router
    route_metrics: RouteMetrics = state.route_metrics

    access = gateway.authorize_token(
        token,
        prompt_text=prompt_text,
        max_tokens=max_tokens,
        endpoint=endpoint,
        model=model_id,
    )

    try:
        lease = await scheduler.admit()
    except SchedulerSaturated as exc:
        access.abort()
        route_metrics.record_shed(route_model)
        telemetry.request_rejected(
            request_id=request_id,
            endpoint=endpoint,
            reason=exc.reason,
            retry_after_s=exc.retry_after_s,
        )
        raise on_saturated(exc) from exc

    # Placement (Block 10): prefix-affinity when enabled+supported, else least-busy.
    affinity_chars = state.settings.prefix_affinity_chars
    prefix_key = prompt_text[:affinity_chars] if affinity_chars > 0 else None
    try:
        registry_lease = router.acquire(route_model, prefix_key, required_features)
    except FeatureUnsupportedError:
        # A request error (400), not a capacity shed: the model has a ready backend
        # but none support the required feature. Bound to placement so it cannot be
        # bypassed by a time-of-check race. Release admission, don't count a shed.
        lease.release()
        access.abort()
        raise
    except NoBackendAvailable as exc:
        lease.release()
        access.abort()
        route_metrics.record_shed(route_model)
        if exc.reason == "busy":
            saturated = SchedulerSaturated("all_backends_busy", retry_after_s=1)
            telemetry.request_rejected(
                request_id=request_id,
                endpoint=endpoint,
                reason=saturated.reason,
                retry_after_s=saturated.retry_after_s,
            )
            raise on_saturated(saturated) from exc
        raise BackendNotReadyError("no backend is ready to generate") from exc

    counters.request_started()
    telemetry.request_start(
        request_id=request_id, endpoint=endpoint, model=model_id, key_id=access.key_id
    )

    try:
        stream = registry_lease.backend.generate(gen_request)
    except BaseException as exc:
        # The route was already selected, so it must be accounted for exactly once
        # with outcome "error" (Block 12.2a) before the leases are released.
        _record_route_outcome(
            route_metrics=route_metrics,
            decision=registry_lease.decision,
            request_id=request_id,
            endpoint=endpoint,
            model=model_id,
            backend=registry_lease.entry.name,
            queue_wait_ms=lease.waited_s * 1000.0,
            prompt_tokens=0,
            completion_tokens=0,
            cost=0.0,
            total_ms=None,
            ttft_ms=None,
            upstream_attempts=0,
            outcome="error",
        )
        registry_lease.release()
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
        registry_lease=registry_lease,
        stream=stream,
        route_metrics=route_metrics,
        request_id=request_id,
        model_id=model_id,
        endpoint=endpoint,
        counters=counters,
        telemetry=telemetry,
        scheduler=scheduler,
    )


def _record_route_outcome(
    *,
    route_metrics: RouteMetrics,
    decision: RouteDecision | None,
    request_id: str,
    endpoint: str,
    model: str,
    backend: str,
    queue_wait_ms: float | None,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
    ttft_ms: float | None,
    total_ms: float | None,
    upstream_attempts: int,
    outcome: str,
) -> None:
    """Record + log one routed terminal outcome (Block 12.2a), used by every path.

    Centralizes route-metrics attribution and the ``route_decision`` structured log
    so field meanings cannot drift between the normal finish and the synchronous
    ``generate()`` failure path. Emits **no** prompt/completion content, credentials,
    headers, or URLs — only the safe routing/latency fields below.
    """
    error = outcome == "error"
    cancelled = outcome == "cancelled"
    route_metrics.record(
        model=model,
        backend=backend,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        total_ms=total_ms,
        ttft_ms=ttft_ms,
        error=error,
        cancelled=cancelled,
        reason=decision.reason if decision else None,
        fallbacks=decision.fallbacks if decision else (),
        policy=decision.policy if decision else None,
        tier=decision.tier if decision else None,
        queue_wait_ms=queue_wait_ms,
        upstream_attempts=upstream_attempts,
        workload_rule=decision.workload_rule if decision else None,
    )
    output_tps = output_tps_sample(
        completion_tokens=completion_tokens, total_ms=total_ms, error=error, cancelled=cancelled
    )
    _log.info(
        "route_decision",
        extra={
            "request_id": request_id,
            "endpoint": endpoint,
            "model": model,
            "backend": backend,
            "engine": decision.engine if decision else None,
            "tier": decision.tier if decision else None,
            "reason": decision.reason if decision else None,
            "fallbacks": list(decision.fallbacks) if decision else [],
            "policy": decision.policy if decision else None,
            "step": decision.step if decision else None,
            "workload_rule": decision.workload_rule if decision else None,
            "queue_wait_ms": round(queue_wait_ms, 1) if queue_wait_ms is not None else None,
            "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
            "output_tps": round(output_tps, 2) if output_tps is not None else None,
            "total_ms": round(total_ms, 1) if total_ms is not None else None,
            "upstream_attempts": upstream_attempts,
            "outcome": outcome,
        },
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
    served.registry_lease.release()

    prompt_tokens = result.prompt_tokens if result else 0
    completion_tokens = result.completion_tokens if result else 0
    entry = served.registry_lease.entry
    cost = (
        prompt_tokens / 1000.0 * entry.cost_per_1k_input
        + completion_tokens / 1000.0 * entry.cost_per_1k_output
    )
    outcome = "cancelled" if cancelled else ("error" if error is not None else "ok")
    _record_route_outcome(
        route_metrics=served.route_metrics,
        decision=served.registry_lease.decision,
        request_id=served.request_id,
        endpoint=served.endpoint,
        model=served.model_id,
        backend=entry.name,
        queue_wait_ms=served.lease.waited_s * 1000.0,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        total_ms=result.timings.total_ms if result else None,
        ttft_ms=result.timings.ttft_ms if result else None,
        upstream_attempts=getattr(served.stream, "upstream_attempts", 0),
        outcome=outcome,
    )
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
