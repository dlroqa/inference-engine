"""Shared core for remote backends that speak an OpenAI-compatible API.

vLLM and SGLang (and other servers) expose the same surface — ``/v1/models``,
``/v1/chat/completions``, ``/v1/completions``, SSE token deltas, and a terminal
usage chunk via ``stream_options``. This module owns everything generic about
proxying to such a server so each concrete adapter is a thin subclass that only
names itself (and, if ever needed, tweaks the request body):

* **Explicit model mapping.** The engine's ``model_id`` is what clients see; the
  backend sends the operator-configured ``remote_model`` upstream. No implicit
  passthrough of arbitrary client-supplied model names.
* **Secure credentials.** An optional API key is sent as a Bearer header to the
  remote *only* and is never logged, echoed in health, or placed in errors.
* **Health.** ``load()`` verifies the remote is reachable and actually serves the
  configured model (via ``/v1/models``) before the backend reports ready.
* **Timeout behavior.** Bounded connect and read timeouts; a stalled remote
  surfaces as an honest terminal error rather than a hang.
* **Safe pre-stream failover only.** A transient failure *before the first token*
  may be retried (bounded); once any token has been sent to the client, an error
  ends the stream honestly — never a silent re-route. Multi-backend failover is a
  later sub-slice.

``httpx`` is imported lazily (the ``remote`` extra) so the core install stays
dependency-light, mirroring how the ``llama`` extra gates the local backend.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engine.inference.base import GenerationStream, InferenceBackend
from engine.inference.types import (
    BackendNotReadyError,
    BackendState,
    Capabilities,
    FinishReason,
    GenerationChunk,
    GenerationFailedError,
    GenerationRequest,
    GenerationResult,
    GenerationTimings,
    ModelLoadError,
)
from engine.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

_log = get_logger("engine.inference.remote")

# HTTP statuses that are worth a bounded retry *before* the first token: the
# remote is transiently unavailable/overloaded rather than rejecting the request.
_RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class _RemoteError(Exception):
    """An upstream HTTP failure, tagged with whether a pre-stream retry is sane."""

    def __init__(self, message: str, *, retriable: bool) -> None:
        super().__init__(message)
        self.retriable = retriable


@dataclass(slots=True)
class _Outcome:
    """Terminal metadata captured from the SSE stream (finish reason + usage)."""

    finish: FinishReason = FinishReason.STOP
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_seen: bool = False


class RemoteGenerationStream(GenerationStream):
    """Adapts an async token producer (remote SSE) onto the stream contract.

    All work is async here (no worker thread): tokens are pulled directly from
    the upstream HTTP stream. The bounded pre-stream retry lives inside the
    producer generator, so the safety rule — no failover once a token has been
    yielded — is enforced in exactly one place.
    """

    def __init__(
        self,
        *,
        request: GenerationRequest,
        tokens: AsyncIterator[str],
        outcome: _Outcome,
    ) -> None:
        self._request = request
        self._tokens = tokens
        self._outcome = outcome
        self._index = 0
        self._parts: list[str] = []
        self._timings = GenerationTimings(started_at=time.monotonic())
        self._result: GenerationResult | None = None
        self._exhausted = False
        self._closed = False

    async def __anext__(self) -> GenerationChunk:
        if self._exhausted:
            raise StopAsyncIteration
        try:
            text = await self._tokens.__anext__()
        except StopAsyncIteration:
            self._build_result(self._outcome.finish)
            raise
        except BaseException as exc:  # transport error / stall after a token
            # No pre-stream retry reaches here (the producer only re-raises once a
            # token has been yielded), so this is an honest terminal failure.
            self._build_result(FinishReason.ERROR)
            raise GenerationFailedError(str(exc)) from exc
        if self._timings.first_token_at is None:
            self._timings.first_token_at = time.monotonic()
        chunk = GenerationChunk(text=text, index=self._index)
        self._index += 1
        self._parts.append(text)
        return chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        aclose = getattr(self._tokens, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        if self._result is None:
            self._build_result(FinishReason.CANCELLED)
        self._exhausted = True

    def _build_result(self, finish: FinishReason) -> None:
        if self._result is not None:
            return
        self._timings.finished_at = time.monotonic()
        outcome = self._outcome
        completion_tokens = outcome.completion_tokens if outcome.usage_seen else self._index
        self._result = GenerationResult(
            request_id=self._request.request_id,
            text="".join(self._parts),
            finish_reason=finish,
            prompt_tokens=outcome.prompt_tokens if outcome.usage_seen else 0,
            completion_tokens=completion_tokens,
            timings=self._timings,
        )
        self._exhausted = True

    @property
    def result(self) -> GenerationResult | None:
        return self._result

    @property
    def prompt_tokens(self) -> int:
        # Remote usage is only known once the terminal usage chunk arrives, so
        # this is 0 until then (honest: the base contract allows 0 = unavailable).
        return self._outcome.prompt_tokens if self._outcome.usage_seen else 0


@dataclass(slots=True)
class RemoteBackendConfig:
    """Immutable connection/mapping settings for a remote OpenAI-compatible backend."""

    base_url: str
    remote_model: str
    model_id: str = "local-model"
    api_key: str | None = None
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 60.0
    max_prestream_retries: int = 1
    context_length: int | None = None
    tls_verify: bool = True
    #: Optional httpx transport override, used only by tests to mount a fake
    #: server. Production code leaves this None so httpx opens a real connection.
    transport: Any = field(default=None, repr=False)


class OpenAICompatibleRemoteBackend(InferenceBackend):
    """Base for remote backends served over an OpenAI-compatible HTTP API.

    Concrete adapters set :attr:`name` and, only if a server needs it, override
    :meth:`_augment_body` to add engine-specific request fields. Everything else
    — lifecycle, health, streaming, retry policy — is shared and identical.
    """

    #: Human-readable adapter name (surfaced in capabilities/health/telemetry).
    name: str = "remote-openai"

    def __init__(self, config: RemoteBackendConfig) -> None:
        self._cfg = config
        self._state = BackendState.UNLOADED
        self._client: httpx.AsyncClient | None = None
        self._context_length = config.context_length or 4096

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> BackendState:
        return self._state

    # -- lifecycle ---------------------------------------------------------

    async def load(self) -> None:
        if self._state in (BackendState.READY, BackendState.GENERATING):
            return
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - exercised via message text
            raise ModelLoadError(
                "httpx is not installed; install the 'remote' extra: "
                'pip install "inference-engine[remote]"'
            ) from exc

        self._state = BackendState.LOADING
        headers = {}
        if self._cfg.api_key:
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"
        timeout = httpx.Timeout(
            connect=self._cfg.connect_timeout_s,
            read=self._cfg.read_timeout_s,
            write=self._cfg.connect_timeout_s,
            pool=self._cfg.connect_timeout_s,
        )
        client = httpx.AsyncClient(
            base_url=_normalize_base_url(self._cfg.base_url),
            headers=headers,
            timeout=timeout,
            verify=self._cfg.tls_verify,
            transport=self._cfg.transport,
        )
        try:
            await self._verify_model(client)
        except Exception as exc:
            await client.aclose()
            self._state = BackendState.UNLOADED
            # ``exc`` may carry an upstream message but never our credentials.
            raise ModelLoadError(str(exc)) from exc
        self._client = client
        self._state = BackendState.READY
        _log.info(
            "remote_backend_ready",
            extra={"backend": self.name, "remote_model": self._cfg.remote_model},
        )

    async def _verify_model(self, client: httpx.AsyncClient) -> None:
        """Confirm the remote is reachable and serves the configured model."""
        resp = await client.get("/v1/models")
        if resp.status_code != 200:
            raise ModelLoadError(f"remote model listing failed: HTTP {resp.status_code}")
        payload = resp.json()
        served = {entry.get("id") for entry in payload.get("data", [])}
        if self._cfg.remote_model not in served:
            available = ", ".join(sorted(s for s in served if s)) or "(none)"
            raise ModelLoadError(
                f"remote does not serve model {self._cfg.remote_model!r}; available: {available}"
            )
        for entry in payload.get("data", []):
            if entry.get("id") == self._cfg.remote_model:
                max_len = entry.get("max_model_len")
                if self._cfg.context_length is None and isinstance(max_len, int):
                    self._context_length = max_len
                break

    async def unload(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        self._state = BackendState.UNLOADED

    def capabilities(self) -> Capabilities:
        if self._state not in (BackendState.READY, BackendState.GENERATING):
            raise BackendNotReadyError("remote backend is not ready")
        return Capabilities(
            backend=self.name,
            model_id=self._cfg.model_id,
            context_length=self._context_length,
            streaming=True,
            max_output_tokens=None,
            # The server may support guided decoding, but mapping it honestly is a
            # later sub-slice; report False so the edge rejects structured requests
            # rather than silently returning unconstrained text.
            supports_structured_output=False,
            extra={"remote_model": self._cfg.remote_model},
        )

    async def check_health(self) -> bool:
        """Lightweight liveness probe for the registry (bounded timeout).

        Returns True iff the backend is loaded and the remote answers ``/v1/models``
        quickly. Secret-free and never raises — a failure just marks the backend
        unavailable so selection routes elsewhere.
        """
        client = self._client
        if client is None or self._state not in (BackendState.READY, BackendState.GENERATING):
            return False
        try:
            resp = await client.get("/v1/models", timeout=5.0)
        except Exception:
            return False
        return resp.status_code == 200

    async def health(self) -> dict[str, object]:
        # Secret-free by construction: base_url and model only, never the key.
        snapshot: dict[str, object] = {
            "backend": self.name,
            "state": self._state.value,
            "base_url": self._cfg.base_url,
            "remote_model": self._cfg.remote_model,
        }
        if self._state in (BackendState.READY, BackendState.GENERATING):
            snapshot["model_id"] = self._cfg.model_id
            snapshot["context_length"] = self._context_length
        return snapshot

    # -- generation --------------------------------------------------------

    def generate(self, request: GenerationRequest) -> GenerationStream:
        if self._state != BackendState.READY:
            raise BackendNotReadyError("remote backend is not ready to generate")
        # A remote server handles its own concurrency; engine-wide admission
        # control (Block 7) still gates overlap, so the backend stays READY and
        # does not lock to a single generation.
        if not request.request_id:
            request = request.model_copy(update={"request_id": uuid.uuid4().hex})
        outcome = _Outcome()
        tokens = self._stream_tokens(request, outcome)
        return RemoteGenerationStream(request=request, tokens=tokens, outcome=outcome)

    def _augment_body(self, request: GenerationRequest, body: dict[str, Any]) -> None:
        """Adapter hook to add engine-specific request fields (default: none).

        vLLM and SGLang both accept the OpenAI-superset body built by
        :meth:`_request_body`, so neither overrides this; it exists so a future
        server with a genuine quirk changes one small method, not the core.
        """

    def _request_body(self, request: GenerationRequest) -> tuple[str, dict[str, Any]]:
        """Build the upstream (path, JSON body) for chat or completion input."""
        body: dict[str, Any] = {
            "model": self._cfg.remote_model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "presence_penalty": request.presence_penalty,
            "frequency_penalty": request.frequency_penalty,
        }
        # OpenAI-superset sampler params accepted by vLLM and SGLang alike.
        if request.top_k:
            body["top_k"] = request.top_k
        if request.min_p:
            body["min_p"] = request.min_p
        if request.repeat_penalty != 1.0:
            body["repetition_penalty"] = request.repeat_penalty
        if request.seed is not None:
            body["seed"] = request.seed
        if request.stop:
            body["stop"] = request.stop
        self._augment_body(request, body)
        if request.is_chat:
            assert request.messages is not None
            body["messages"] = [{"role": m.role, "content": m.content} for m in request.messages]
            return "/v1/chat/completions", body
        body["prompt"] = request.prompt
        return "/v1/completions", body

    async def _stream_tokens(
        self, request: GenerationRequest, outcome: _Outcome
    ) -> AsyncIterator[str]:
        """Yield token text from the remote, retrying only before the first token."""
        import httpx

        assert self._client is not None
        path, body = self._request_body(request)
        headers = {"X-Request-Id": request.request_id} if request.request_id else {}
        is_chat = request.is_chat
        attempts = 0
        yielded_any = False
        while True:
            try:
                async with self._client.stream("POST", path, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        raise _RemoteError(
                            f"remote generation failed: HTTP {resp.status_code}",
                            retriable=resp.status_code in _RETRIABLE_STATUS,
                        )
                    async for line in resp.aiter_lines():
                        for text in _parse_sse_line(line, is_chat=is_chat, outcome=outcome):
                            if text is _DONE:
                                return
                            yielded_any = True
                            yield text
                return
            except (httpx.TransportError, _RemoteError) as exc:
                retriable = not isinstance(exc, _RemoteError) or exc.retriable
                if yielded_any or not retriable or attempts >= self._cfg.max_prestream_retries:
                    # Once a token is out, or the error is not retriable, or we are
                    # out of pre-stream attempts: fail honestly, never re-route.
                    raise
                attempts += 1
                _log.warning(
                    "remote_prestream_retry",
                    extra={
                        "backend": self.name,
                        "attempt": attempts,
                        "request_id": request.request_id,
                    },
                )


# Sentinel yielded by the SSE parser to signal the terminal ``[DONE]`` marker.
_DONE = object()


def _normalize_base_url(base_url: str) -> str:
    """Server root for the OpenAI paths.

    Accepts either the server root (``http://host:8000``) or the OpenAI base that
    operators usually configure (``http://host:8000/v1``); the backend always
    builds absolute ``/v1/...`` paths, so a trailing ``/v1`` is stripped to avoid
    a doubled ``/v1/v1``.
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def _parse_sse_line(line: str, *, is_chat: bool, outcome: _Outcome) -> list[Any]:
    """Parse one SSE line into a list of token strings (or the DONE sentinel).

    Returns an empty list for comments/keepalives/usage-only chunks, a single
    token string for a content delta, or ``[_DONE]`` at end of stream. Finish
    reason and usage are recorded onto ``outcome`` as a side effect.
    """
    line = line.strip()
    if not line or line.startswith(":") or not line.startswith("data:"):
        return []
    data = line[len("data:") :].strip()
    if data == "[DONE]":
        return [_DONE]
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return []

    usage = obj.get("usage")
    if isinstance(usage, dict):
        outcome.prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        outcome.completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        outcome.usage_seen = True

    choices = obj.get("choices") or []
    if not choices:
        return []
    choice = choices[0]
    reason = choice.get("finish_reason")
    if reason == "length":
        outcome.finish = FinishReason.LENGTH
    elif reason in ("stop", "eos_token"):
        outcome.finish = FinishReason.STOP
    if is_chat:
        text = (choice.get("delta") or {}).get("content") or ""
    else:
        text = choice.get("text") or ""
    return [text] if text else []
