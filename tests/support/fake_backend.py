"""A fake backend for tests.

It reuses the real :class:`ThreadedBackend` state machine and
:class:`QueueGenerationStream` async bridge, but produces tokens from a list in a
blocking worker thread (optionally with a per-token delay). This lets the Block 1
behavioral tests exercise threading, ordering, cancellation, and event-loop
responsiveness without needing llama.cpp or a real model.
"""

from __future__ import annotations

import threading
import time

from engine.inference.threaded import ThreadedBackend
from engine.inference.types import (
    Capabilities,
    FinishReason,
    GenerationRequest,
    TokenGenerator,
)


class FakeBackend(ThreadedBackend):
    name = "fake"

    def __init__(
        self,
        *,
        tokens: list[str] | None = None,
        per_token_delay: float = 0.0,
        fail_on_load: bool = False,
        fail_after: int | None = None,
        model_id: str = "fake-model",
        n_ctx: int = 2048,
    ) -> None:
        super().__init__()
        self._tokens = tokens if tokens is not None else ["Hello", ",", " ", "world", "!"]
        self._delay = per_token_delay
        self._fail_on_load = fail_on_load
        self._fail_after = fail_after
        self._model_id = model_id
        self._n_ctx = n_ctx
        self.loaded = False

    def _load(self) -> None:
        if self._fail_on_load:
            raise RuntimeError("simulated load failure")
        self.loaded = True

    def _unload(self) -> None:
        self.loaded = False

    def _capabilities(self) -> Capabilities:
        return Capabilities(backend=self.name, model_id=self._model_id, context_length=self._n_ctx)

    def _count_prompt_tokens(self, request: GenerationRequest) -> int:
        return len(request.prompt_text().split())

    def _token_producer(
        self, request: GenerationRequest, cancel: threading.Event
    ) -> TokenGenerator:
        emitted = 0
        for tok in self._tokens:
            if cancel.is_set():
                return FinishReason.CANCELLED
            if self._fail_after is not None and emitted >= self._fail_after:
                raise RuntimeError("simulated generation failure")
            if self._delay:
                slept = 0.0
                while slept < self._delay:
                    if cancel.is_set():
                        return FinishReason.CANCELLED
                    step = min(0.01, self._delay - slept)
                    time.sleep(step)
                    slept += step
            yield tok
            emitted += 1
        return FinishReason.STOP
