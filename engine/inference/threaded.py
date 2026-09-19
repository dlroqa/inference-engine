"""Base class for backends whose work is blocking and runs in a worker thread.

Owns the explicit lifecycle state machine (unloaded → loading → ready →
generating → failed/ready) and the single-model, single-generation invariants for
Block 1. Subclasses implement only the model-specific hooks; the async bridging,
cancellation, timing, and state transitions live here.
"""

from __future__ import annotations

import asyncio
import threading
import uuid

from engine.inference.base import GenerationStream, InferenceBackend
from engine.inference.stream import QueueGenerationStream
from engine.inference.types import (
    BackendBusyError,
    BackendNotReadyError,
    BackendState,
    Capabilities,
    GenerationRequest,
    ModelLoadError,
    TokenGenerator,
)


class ThreadedBackend(InferenceBackend):
    """Manages state + threading; delegates model specifics to subclass hooks."""

    #: Human-readable backend name, overridden by subclasses.
    name: str = "threaded"

    def __init__(self) -> None:
        self._state = BackendState.UNLOADED
        self._lock = threading.Lock()
        self._active: QueueGenerationStream | None = None

    # -- subclass hooks ----------------------------------------------------

    def _load(self) -> None:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _unload(self) -> None:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _capabilities(self) -> Capabilities:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def _count_prompt_tokens(self, request: GenerationRequest) -> int:  # pragma: no cover
        raise NotImplementedError

    def _token_producer(
        self, request: GenerationRequest, cancel: threading.Event
    ) -> TokenGenerator:  # pragma: no cover - abstract-ish
        raise NotImplementedError
        yield ""  # pragma: no cover - marks this as a generator function

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> BackendState:
        return self._state

    # -- lifecycle ---------------------------------------------------------

    async def load(self) -> None:
        with self._lock:
            if self._state in (BackendState.READY, BackendState.GENERATING):
                return
            if self._state == BackendState.LOADING:
                raise BackendBusyError("model is already loading")
            self._state = BackendState.LOADING

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._load)
        except Exception as exc:
            # Leave the backend recoverable, not stuck in LOADING.
            self._state = BackendState.UNLOADED
            raise ModelLoadError(str(exc)) from exc
        self._state = BackendState.READY

    async def unload(self) -> None:
        active = self._active
        if active is not None:
            await active.aclose()
            self._active = None

        if self._state == BackendState.UNLOADED:
            return

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._unload)
        finally:
            self._state = BackendState.UNLOADED

    def capabilities(self) -> Capabilities:
        if self._state not in (BackendState.READY, BackendState.GENERATING):
            raise BackendNotReadyError("no model is loaded")
        return self._capabilities()

    async def health(self) -> dict[str, object]:
        snapshot: dict[str, object] = {"backend": self.name, "state": self._state.value}
        if self._state in (BackendState.READY, BackendState.GENERATING):
            caps = self._capabilities()
            snapshot["model_id"] = caps.model_id
            snapshot["context_length"] = caps.context_length
        return snapshot

    # -- generation --------------------------------------------------------

    def generate(self, request: GenerationRequest) -> GenerationStream:
        with self._lock:
            if self._state == BackendState.GENERATING:
                raise BackendBusyError("a generation is already in progress")
            if self._state != BackendState.READY:
                raise BackendNotReadyError("no model is ready to generate")
            self._state = BackendState.GENERATING

        if not request.request_id:
            request = request.model_copy(update={"request_id": uuid.uuid4().hex})

        try:
            prompt_tokens = self._count_prompt_tokens(request)
        except Exception:
            self._state = BackendState.READY
            raise

        def producer(cancel: threading.Event) -> TokenGenerator:
            return self._token_producer(request, cancel)

        stream = QueueGenerationStream(
            request=request,
            prompt_tokens=prompt_tokens,
            producer=producer,
            on_done=self._on_generation_done,
        )
        self._active = stream
        return stream

    def _on_generation_done(self) -> None:
        self._active = None
        if self._state == BackendState.GENERATING:
            self._state = BackendState.READY
