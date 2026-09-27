"""gRPC edge for the Inference Engine (Block 10, sub-slice 6).

Exposes the same serving pipeline as the HTTP edges — auth, admission control,
virtual-model routing, spillover, generation, and per-route metering — over a
gRPC ``InferenceService`` on its own port. It reuses ``serving.start_generation_core``
and ``finish_generation`` verbatim, so nothing bypasses the gateway or registry;
only the transport (and API-key source: call metadata) differs.

``grpcio`` is imported here at module load, so this module is imported lazily
(only when ``grpc_enabled``), mirroring how the ``llama``/``remote`` extras gate
their native deps. Kubernetes topology, MoE, and prefill/decode disaggregation are
deferred until benchmarked (see docs/grpc.md).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import grpc

from engine.api.errors import OpenAIError
from engine.api.serving import Served, finish_generation, start_generation_core
from engine.grpc import inference_pb2 as _pb
from engine.grpc import inference_pb2_grpc as _pb_grpc
from engine.inference.scheduler import SchedulerSaturated
from engine.inference.types import (
    BackendNotReadyError,
    FeatureUnsupportedError,
    GenerationFailedError,
    GenerationRequest,
    Message,
)
from engine.logging_setup import get_logger

_log = get_logger("engine.grpc")

# The generated protobuf modules are dynamically typed (no mypy stubs); treat
# them as Any so this glue type-checks without mypy-protobuf.
pb: Any = _pb
pb_grpc: Any = _pb_grpc

# Map an OpenAI-edge HTTP status onto the closest gRPC status code.
_HTTP_TO_GRPC = {
    400: grpc.StatusCode.INVALID_ARGUMENT,
    401: grpc.StatusCode.UNAUTHENTICATED,
    403: grpc.StatusCode.PERMISSION_DENIED,
    404: grpc.StatusCode.NOT_FOUND,
    413: grpc.StatusCode.RESOURCE_EXHAUSTED,
    429: grpc.StatusCode.RESOURCE_EXHAUSTED,
    503: grpc.StatusCode.UNAVAILABLE,
}


class _Saturated(Exception):
    """Sentinel raised via the on_saturated hook -> RESOURCE_EXHAUSTED."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__("engine saturated")
        self.retry_after_s = retry_after_s


def _token_from_metadata(context: grpc.aio.ServicerContext) -> str | None:
    """Extract an API key from call metadata (authorization bearer / x-api-key)."""
    md = dict(context.invocation_metadata() or [])
    auth = md.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[len("bearer ") :].strip()
    x_api_key = md.get("x-api-key")
    if x_api_key:
        return x_api_key.strip()
    return None


def _build_request(request: pb.GenerateRequest, request_id: str) -> tuple[GenerationRequest, str]:
    """Translate the proto request onto the internal GenerationRequest + prompt text.

    proto3 scalars can't distinguish unset from zero, so 0 means "use the default"
    for sampling knobs (documented in docs/grpc.md); use a small value for greedy.
    """
    max_tokens = request.max_tokens if request.max_tokens > 0 else None
    common: dict[str, Any] = {
        "request_id": request_id,
        "max_tokens": max_tokens,
        "temperature": request.temperature if request.temperature > 0 else 0.8,
        "top_p": request.top_p if request.top_p > 0 else 0.95,
        "top_k": request.top_k if request.top_k > 0 else 40,
        "stop": list(request.stop),
    }
    if request.messages:
        gen = GenerationRequest(
            messages=[Message(role=m.role, content=m.content) for m in request.messages],
            **common,
        )
        return gen, "\n".join(m.content for m in request.messages)
    return GenerationRequest(prompt=request.prompt, **common), request.prompt


class _Handler:
    """One ``Generate`` call: the task running it and its response stream."""

    def __init__(self, task: asyncio.Task[Any] | None) -> None:
        self.task = task
        self.stream: AsyncGenerator[Any, None] | None = None
        self._lock = asyncio.Lock()

    async def finish(self) -> None:
        """Closes the response stream, which finalizes the request if it has not
        been finalized yet (an idle, finished stream closes as a no-op)."""
        async with self._lock:
            if self.stream is not None:
                await self.stream.aclose()


class _Handlers:
    """Tracks every live ``Generate`` handler until its final usage write is done.

    ``grpc.aio`` iterates a streaming handler from its own per-call task and
    awaits each write outside the generator. A call cancelled during a write
    leaves the generator suspended at a ``yield``: nothing finalizes it until
    garbage collection does, and ``Server.stop()`` does not wait for that.
    """

    def __init__(self) -> None:
        self._open: set[_Handler] = set()

    def open(self) -> _Handler:
        handler = _Handler(asyncio.current_task())
        self._open.add(handler)
        return handler

    async def close(self, handler: _Handler) -> None:
        try:
            await handler.finish()
        finally:
            self._open.discard(handler)

    async def close_all(self) -> None:
        """Returns once every tracked handler has ended and been finalized.

        Call after the server has stopped (no new calls). Waits for each
        handler's task without cancelling it again, then closes any stream it
        abandoned. There is no time limit; a caller that is cancelled here has
        not established that the handlers ended.
        """
        while self._open:
            handlers = list(self._open)
            current = asyncio.current_task()
            tasks = [h.task for h in handlers if h.task is not None]
            pending = {t for t in tasks if t is not current and not t.done()}
            if pending:
                await asyncio.wait(pending)
            for handler in handlers:
                await self.close(handler)


class InferenceServicer(pb_grpc.InferenceServiceServicer):
    """Implements InferenceService against the shared serving pipeline."""

    def __init__(self, app: object) -> None:
        self._app = app
        self.handlers = _Handlers()

    async def ListModels(
        self, request: pb.ListModelsRequest, context: grpc.aio.ServicerContext
    ) -> pb.ListModelsResponse:
        names = self._app.state.router.model_names()  # type: ignore[attr-defined]
        return pb.ListModelsResponse(models=names)

    async def Generate(
        self, request: pb.GenerateRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[pb.GenerateChunk]:
        handler = self.handlers.open()
        try:
            async for chunk in self._generate(request, context, handler):
                yield chunk
        finally:
            await self.handlers.close(handler)

    async def _generate(
        self, request: pb.GenerateRequest, context: grpc.aio.ServicerContext, handler: _Handler
    ) -> AsyncIterator[pb.GenerateChunk]:
        request_id = f"grpc-{uuid.uuid4().hex}"
        model = request.model
        router = self._app.state.router  # type: ignore[attr-defined]
        if not router.known_model(model):
            await context.abort(grpc.StatusCode.NOT_FOUND, f"model {model!r} not found")
        gen_request, prompt_text = _build_request(request, request_id)

        def _on_saturated(exc: SchedulerSaturated) -> Exception:
            return _Saturated(exc.retry_after_s)

        try:
            served = await start_generation_core(
                self._app,
                token=_token_from_metadata(context),
                gen_request=gen_request,
                endpoint="/grpc/Generate",
                model_id=model,
                route_model=model,
                prompt_text=prompt_text,
                max_tokens=gen_request.max_tokens or 256,
                request_id=request_id,
                on_saturated=_on_saturated,
                required_features=frozenset(),
            )
        except _Saturated as exc:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"engine at capacity; retry after {exc.retry_after_s}s",
            )
            return
        except OpenAIError as exc:
            code = _HTTP_TO_GRPC.get(exc.status_code, grpc.StatusCode.INTERNAL)
            await context.abort(code, str(exc.message))
            return
        except BackendNotReadyError as exc:
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
            return
        except FeatureUnsupportedError as exc:  # defensive: gRPC requests no features
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return

        stream = _stream(served, request_id, context)
        handler.stream = stream  # from here, closing it finalizes the request
        async for chunk in stream:
            yield chunk


async def _stream(
    served: Served, request_id: str, context: grpc.aio.ServicerContext
) -> AsyncGenerator[pb.GenerateChunk, None]:
    """Stream token deltas, then a terminal chunk; clean up on every exit path."""
    error: BaseException | None = None
    failed = False
    try:
        async for chunk in served.stream:
            yield pb.GenerateChunk(text=chunk.text, request_id=request_id)
    except GenerationFailedError as exc:  # honest terminal failure mid-stream
        error = exc
        failed = True
    except BaseException as exc:  # client cancel / disconnect
        # An interrupted consume counts as a disconnect (cancelled), not an error.
        await _drain_and_finish(served, error=None)
        raise exc
    result = await _drain_and_finish(served, error=error)
    if failed:
        await context.abort(grpc.StatusCode.INTERNAL, "generation failed")
    yield pb.GenerateChunk(
        done=True,
        request_id=request_id,
        finish_reason=result.finish_reason.value if result else "stop",
        prompt_tokens=result.prompt_tokens if result else 0,
        completion_tokens=result.completion_tokens if result else 0,
    )


async def _drain_and_finish(served: Served, *, error: BaseException | None) -> Any:
    try:
        await served.stream.aclose()
    except Exception:  # pragma: no cover - best-effort teardown
        pass
    return finish_generation(served, error=error)


class GrpcEdge:
    """The gRPC server and its request handlers, stopped as one unit."""

    def __init__(self, server: grpc.aio.Server, servicer: InferenceServicer) -> None:
        self.server = server
        self.servicer = servicer

    async def start(self) -> None:
        await self.server.start()

    async def stop(self, grace: float | None) -> None:
        """Stops the server, then waits until every handler has ended.

        ``Server.stop`` returns once the gRPC core has no live calls, which does
        not prove that a handler's final usage write has run. This returns only
        after it has; if it raises or is cancelled, that is not established.
        """
        await self.server.stop(grace)
        await self.servicer.handlers.close_all()


async def create_grpc_server(
    app: object,
    *,
    host: str,
    port: int,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
) -> tuple[GrpcEdge, int]:
    """Build (not start) a gRPC server bound to host:port; return it and the port.

    With both a cert and key an mTLS-ready secure port is used; otherwise an
    insecure port (front it with a TLS-terminating proxy — see docs/grpc.md).
    """
    server = grpc.aio.server()
    servicer = InferenceServicer(app)
    pb_grpc.add_InferenceServiceServicer_to_server(servicer, server)
    address = f"{host}:{port}"
    if tls_cert and tls_key:
        creds = grpc.ssl_server_credentials(
            [(Path(tls_key).read_bytes(), Path(tls_cert).read_bytes())]
        )
        bound = server.add_secure_port(address, creds)
    else:
        bound = server.add_insecure_port(address)
    return GrpcEdge(server, servicer), bound
