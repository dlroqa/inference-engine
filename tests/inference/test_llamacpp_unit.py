"""Unit tests for the llama.cpp adapter using a fake ``llama_cpp`` module.

These cover the adapter's own logic (load, tokenize, streaming translation,
finish-reason mapping, unload) without a native build or a real model. The real
end-to-end path is covered by the gated integration test + CI job.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from engine.inference.llamacpp import LlamaCppBackend
from engine.inference.types import BackendState, FinishReason, GenerationRequest, ModelLoadError


class _FakeLlama:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def tokenize(self, data: bytes) -> list[int]:
        return list(range(len(data.split())))

    def create_completion(self, **kwargs: object):  # type: ignore[no-untyped-def]
        pieces = ["Hel", "lo", "!"]
        for i, text in enumerate(pieces):
            last = i == len(pieces) - 1
            yield {"choices": [{"text": text, "finish_reason": "length" if last else None}]}


@pytest.fixture
def fake_llama(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("llama_cpp")
    module.Llama = _FakeLlama  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", module)


def _model_file(tmp_path: Path) -> Path:
    p = tmp_path / "tiny.gguf"
    p.write_bytes(b"GGUF fake")
    return p


def test_load_generate_unload_with_fake(tmp_path: Path, fake_llama: None) -> None:
    async def body() -> None:
        backend = LlamaCppBackend(model_path=_model_file(tmp_path), n_ctx=256)
        await backend.load()
        assert backend.state == BackendState.READY
        assert backend.capabilities().context_length == 256

        req = GenerationRequest(prompt="a b c", max_tokens=8)
        result = await backend.generate(req).collect()
        assert result.text == "Hello!"
        assert result.completion_tokens == 3
        assert result.prompt_tokens == 3
        assert result.finish_reason == FinishReason.LENGTH

        await backend.unload()
        assert backend.state == BackendState.UNLOADED

    asyncio.run(body())


def test_missing_model_file_raises(tmp_path: Path, fake_llama: None) -> None:
    async def body() -> None:
        backend = LlamaCppBackend(model_path=tmp_path / "absent.gguf")
        with pytest.raises(ModelLoadError):
            await backend.load()
        assert backend.state == BackendState.UNLOADED

    asyncio.run(body())


def test_missing_llama_package_raises(tmp_path: Path) -> None:
    # llama_cpp is genuinely not installed in the unit environment.
    async def body() -> None:
        backend = LlamaCppBackend(model_path=_model_file(tmp_path))
        with pytest.raises(ModelLoadError) as excinfo:
            await backend.load()
        assert "llama-cpp-python" in str(excinfo.value)
        assert backend.state == BackendState.UNLOADED

    asyncio.run(body())
