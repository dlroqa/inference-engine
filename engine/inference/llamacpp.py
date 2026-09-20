"""llama.cpp backend via ``llama-cpp-python`` (Tier 1, the cross-platform default).

``llama-cpp-python`` is an **optional** dependency (install with the ``llama``
extra) so the Block 0 foundation stays zero-build. It is imported lazily inside
``_load`` so merely importing this module never requires the package.

Only Block 1 scope is implemented: one GGUF model, one generation at a time, a
blocking generation loop isolated in a worker thread (see ``ThreadedBackend`` /
``QueueGenerationStream``), explicit load/unload state, and basic token counts.
Multiple models, parallel slots, continuous batching, GPU auto-tuning, and
downloads are later blocks.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from engine.inference.threaded import ThreadedBackend
from engine.inference.types import (
    Capabilities,
    FinishReason,
    GenerationRequest,
    ModelLoadError,
    TokenGenerator,
)


def _close(stream: object) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        close()


class LlamaCppBackend(ThreadedBackend):
    """A single-model llama.cpp backend."""

    name = "llama.cpp"

    def __init__(
        self,
        *,
        model_path: Path,
        model_id: str = "local-model",
        n_ctx: int = 4096,
        n_threads: int | None = None,
        n_gpu_layers: int = 0,
    ) -> None:
        super().__init__()
        self._model_path = Path(model_path)
        self._model_id = model_id
        self._n_ctx = n_ctx
        self._n_threads = n_threads
        self._n_gpu_layers = n_gpu_layers
        self._llm: Any | None = None

    # -- lifecycle hooks ---------------------------------------------------

    def _load(self) -> None:
        if not self._model_path.is_file():
            raise ModelLoadError(f"model file not found: {self._model_path}")
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise ModelLoadError(
                "llama-cpp-python is not installed; install the 'llama' extra: "
                'pip install "inference-engine[llama]"'
            ) from exc

        self._llm = Llama(
            model_path=str(self._model_path),
            n_ctx=self._n_ctx,
            n_threads=self._n_threads,
            n_gpu_layers=self._n_gpu_layers,
            verbose=False,
        )

    def _unload(self) -> None:
        # Drop the reference; llama.cpp frees native state when the object is GC'd.
        self._llm = None

    def _capabilities(self) -> Capabilities:
        return Capabilities(
            backend=self.name,
            model_id=self._model_id,
            context_length=self._n_ctx,
            streaming=True,
            max_output_tokens=None,
            extra={"n_gpu_layers": self._n_gpu_layers},
        )

    def _count_prompt_tokens(self, request: GenerationRequest) -> int:
        assert self._llm is not None
        # For chat requests this is an estimate over the concatenated message
        # text (the exact templated token count is internal to llama.cpp).
        tokens = self._llm.tokenize(request.prompt_text().encode("utf-8"))
        return len(tokens)

    # -- generation --------------------------------------------------------

    def _token_producer(
        self, request: GenerationRequest, cancel: threading.Event
    ) -> TokenGenerator:
        assert self._llm is not None
        if request.is_chat:
            return self._chat_producer(request, cancel)
        return self._completion_producer(request, cancel)

    def _completion_producer(
        self, request: GenerationRequest, cancel: threading.Event
    ) -> TokenGenerator:
        assert self._llm is not None
        stream = self._llm.create_completion(
            prompt=request.prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            top_k=request.top_k,
            seed=request.seed,
            stop=request.stop or None,
            stream=True,
        )
        finish = FinishReason.STOP
        try:
            for item in stream:
                if cancel.is_set():
                    finish = FinishReason.CANCELLED
                    break
                choice = item["choices"][0]
                text = choice.get("text", "")
                if text:
                    yield text
                if choice.get("finish_reason") == "length":
                    finish = FinishReason.LENGTH
        finally:
            _close(stream)
        return finish

    def _chat_producer(self, request: GenerationRequest, cancel: threading.Event) -> TokenGenerator:
        assert self._llm is not None
        assert request.messages is not None
        stream = self._llm.create_chat_completion(
            messages=[{"role": m.role, "content": m.content} for m in request.messages],
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            top_k=request.top_k,
            seed=request.seed,
            stop=request.stop or None,
            stream=True,
        )
        finish = FinishReason.STOP
        try:
            for item in stream:
                if cancel.is_set():
                    finish = FinishReason.CANCELLED
                    break
                choice = item["choices"][0]
                text = choice.get("delta", {}).get("content", "") or ""
                if text:
                    yield text
                if choice.get("finish_reason") == "length":
                    finish = FinishReason.LENGTH
        finally:
            _close(stream)
        return finish
