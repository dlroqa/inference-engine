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
        self.last_kwargs = kwargs
        pieces = ["Hel", "lo", "!"]
        for i, text in enumerate(pieces):
            last = i == len(pieces) - 1
            yield {"choices": [{"text": text, "finish_reason": "length" if last else None}]}

    def create_chat_completion(self, **kwargs: object):  # type: ignore[no-untyped-def]
        self.last_kwargs = kwargs
        pieces = ["Hi", " there"]
        # First chunk mimics the role-only delta OpenAI emits.
        yield {"choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]}
        for i, text in enumerate(pieces):
            last = i == len(pieces) - 1
            yield {
                "choices": [{"delta": {"content": text}, "finish_reason": "stop" if last else None}]
            }


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


def test_chat_generate_with_fake(tmp_path: Path, fake_llama: None) -> None:
    from engine.inference.types import Message

    async def body() -> None:
        backend = LlamaCppBackend(model_path=_model_file(tmp_path), n_ctx=256)
        await backend.load()
        req = GenerationRequest(messages=[Message(role="user", content="hi there")], max_tokens=8)
        result = await backend.generate(req).collect()
        assert result.text == "Hi there"  # role-only delta is skipped
        assert result.completion_tokens == 2
        assert result.prompt_tokens == 2  # estimate over "hi there"
        assert result.finish_reason == FinishReason.STOP
        await backend.unload()

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


class _FakeGrammar:
    @classmethod
    def from_string(cls, text: str) -> _FakeGrammar:
        g = cls()
        g.text = text  # type: ignore[attr-defined]
        return g


@pytest.fixture
def fake_llama_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("llama_cpp")
    module.Llama = _FakeLlama  # type: ignore[attr-defined]
    module.LlamaGrammar = _FakeGrammar  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", module)


def test_sampler_kwargs_are_mapped(tmp_path: Path, fake_llama: None) -> None:
    async def body() -> None:
        backend = LlamaCppBackend(model_path=_model_file(tmp_path), n_ctx=256)
        await backend.load()
        req = GenerationRequest(
            prompt="a b",
            max_tokens=8,
            temperature=0.3,
            top_p=0.5,
            top_k=10,
            min_p=0.05,
            repeat_penalty=1.1,
            presence_penalty=0.2,
            frequency_penalty=0.4,
            mirostat_mode=2,
            mirostat_tau=4.0,
            mirostat_eta=0.2,
            stop=["END"],
        )
        await backend.generate(req).collect()
        kw = backend._llm.last_kwargs  # type: ignore[union-attr]
        assert kw["temperature"] == 0.3
        assert kw["top_p"] == 0.5
        assert kw["top_k"] == 10
        assert kw["min_p"] == 0.05
        assert kw["repeat_penalty"] == 1.1
        assert kw["presence_penalty"] == 0.2
        assert kw["frequency_penalty"] == 0.4
        assert kw["mirostat_mode"] == 2
        assert kw["mirostat_tau"] == 4.0
        assert kw["stop"] == ["END"]

    asyncio.run(body())


def test_mirostat_off_omits_mirostat_kwargs(tmp_path: Path, fake_llama: None) -> None:
    async def body() -> None:
        backend = LlamaCppBackend(model_path=_model_file(tmp_path), n_ctx=256)
        await backend.load()
        await backend.generate(GenerationRequest(prompt="a", max_tokens=4)).collect()
        kw = backend._llm.last_kwargs  # type: ignore[union-attr]
        assert "mirostat_mode" not in kw

    asyncio.run(body())


def test_structured_output_capability_and_mapping(
    tmp_path: Path, fake_llama_structured: None
) -> None:
    async def body() -> None:
        backend = LlamaCppBackend(model_path=_model_file(tmp_path), n_ctx=256)
        await backend.load()
        assert backend.capabilities().supports_structured_output is True

        # json_object -> response_format
        await backend.generate(
            GenerationRequest(prompt="a", max_tokens=4, json_object=True)
        ).collect()
        assert backend._llm.last_kwargs["response_format"] == {"type": "json_object"}  # type: ignore[union-attr]

        # json_schema -> response_format with schema
        schema = {"type": "object"}
        await backend.generate(
            GenerationRequest(prompt="a", max_tokens=4, json_schema=schema)
        ).collect()
        assert backend._llm.last_kwargs["response_format"]["schema"] == schema  # type: ignore[union-attr]

        # grammar -> compiled LlamaGrammar
        await backend.generate(
            GenerationRequest(prompt="a", max_tokens=4, grammar='root ::= "x"')
        ).collect()
        assert backend._llm.last_kwargs["grammar"].text == 'root ::= "x"'  # type: ignore[union-attr]

    asyncio.run(body())


def test_structured_output_unsupported_without_grammar_class(
    tmp_path: Path, fake_llama: None
) -> None:
    async def body() -> None:
        backend = LlamaCppBackend(model_path=_model_file(tmp_path), n_ctx=256)
        await backend.load()
        # The base fake module has no LlamaGrammar, so structured output is off.
        assert backend.capabilities().supports_structured_output is False

    asyncio.run(body())
