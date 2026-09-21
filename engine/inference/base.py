"""The ``InferenceBackend`` interface and the ``GenerationStream`` contract.

Every concrete backend (llama.cpp now; vLLM/SGLang/remote later) implements this
same interface, so the API edge and CLI never change when the backend does. The
interface exposes only what Block 1 needs: load, unload, state/health,
capabilities, and an async token stream. Multiple resident models, parallel
slots, routing, and downloads are later blocks.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator

from engine.inference.types import (
    BackendState,
    Capabilities,
    GenerationChunk,
    GenerationRequest,
    GenerationResult,
)


class GenerationStream(abc.ABC):
    """An async iterator of :class:`GenerationChunk` with a terminal result.

    Usage::

        stream = backend.generate(request)
        async for chunk in stream:
            ...
        result = stream.result   # available once iteration completes

    Closing the stream (``await stream.aclose()``) cancels an in-flight
    generation and releases the worker.
    """

    def __aiter__(self) -> AsyncIterator[GenerationChunk]:
        return self

    @abc.abstractmethod
    async def __anext__(self) -> GenerationChunk: ...

    @abc.abstractmethod
    async def aclose(self) -> None: ...

    @property
    @abc.abstractmethod
    def result(self) -> GenerationResult | None:
        """The terminal result, or ``None`` until the stream is exhausted."""

    @property
    def backpressure_waits(self) -> int:
        """How many times token production blocked on a full buffer (slow consumer).

        Zero for backends without a bounded token buffer; the queue-backed stream
        overrides it. Read after the stream is exhausted/closed.
        """
        return 0

    @property
    def prompt_tokens(self) -> int:
        """Prompt token count, known once generation starts (0 if unavailable).

        Available before the terminal result so an edge can report input-token
        usage in an opening streaming event (e.g. Anthropic ``message_start``).
        """
        return 0

    async def collect(self) -> GenerationResult:
        """Consume the whole stream and return the terminal result.

        Convenience for non-streaming callers; the same normalized pipeline.
        """
        async for _ in self:
            pass
        assert self.result is not None
        return self.result


class InferenceBackend(abc.ABC):
    """Interface for a single-model inference backend."""

    @property
    @abc.abstractmethod
    def state(self) -> BackendState: ...

    @abc.abstractmethod
    async def load(self) -> None:
        """Load the configured model. Idempotent when already ready."""

    @abc.abstractmethod
    async def unload(self) -> None:
        """Unload the model and free runtime state. Idempotent."""

    @abc.abstractmethod
    def capabilities(self) -> Capabilities:
        """Describe the loaded model. Raises if not ready."""

    @abc.abstractmethod
    async def health(self) -> dict[str, object]:
        """A small, secret-free health/status snapshot."""

    @abc.abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationStream:
        """Start a generation and return its stream. Raises if not ready/busy."""
