"""Integration test for the real llama.cpp backend.

Skipped unless BOTH are available:
  - ``llama-cpp-python`` is importable, and
  - ``IE_TEST_GGUF`` points at a small GGUF model file.

CI provisions these in a dedicated job (see ``.github/workflows/ci.yml``); local
developers can run it by exporting ``IE_TEST_GGUF=/path/to/tiny-model.gguf``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path

import pytest

from engine.inference.llamacpp import LlamaCppBackend
from engine.inference.types import BackendState, FinishReason, GenerationRequest

_GGUF = os.environ.get("IE_TEST_GGUF")
_HAS_LLAMA = importlib.util.find_spec("llama_cpp") is not None

pytestmark = pytest.mark.skipif(
    not (_HAS_LLAMA and _GGUF and Path(_GGUF).is_file()),
    reason="requires llama-cpp-python and IE_TEST_GGUF pointing at a GGUF file",
)


def test_load_generate_unload_real_model() -> None:
    async def body() -> None:
        backend = LlamaCppBackend(model_path=Path(_GGUF), n_ctx=512)
        await backend.load()
        assert backend.state == BackendState.READY
        caps = backend.capabilities()
        assert caps.context_length == 512

        # A strong base-continuation prompt so even a tiny model emits tokens.
        stream = backend.generate(
            GenerationRequest(prompt="The capital of France is", max_tokens=16, temperature=0.0)
        )
        chunks = [c async for c in stream]
        assert chunks  # produced at least one token
        assert [c.index for c in chunks] == list(range(len(chunks)))
        result = stream.result
        assert result is not None
        assert result.finish_reason in (FinishReason.STOP, FinishReason.LENGTH)
        assert result.completion_tokens == len(chunks)
        assert result.prompt_tokens > 0

        await backend.unload()
        assert backend.state == BackendState.UNLOADED

    asyncio.run(body())
