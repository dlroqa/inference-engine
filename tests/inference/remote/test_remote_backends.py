"""Tests for the remote OpenAI-compatible backends (Block 10, sub-slices 1-2).

The vLLM and SGLang adapters share one core (``OpenAICompatibleRemoteBackend``),
so the behavioral tests are parametrized over both classes and run against a real
fake-server Uvicorn instance (genuine HTTP transport — streaming, mid-stream
disconnects, status codes). Each async body is wrapped in ``asyncio.run`` (no
plugin dep), matching the rest of the inference suite.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from engine.inference.remote import (
    OpenAICompatibleRemoteBackend,
    RemoteBackendConfig,
    RemoteSGLangBackend,
    RemoteVLLMBackend,
)
from engine.inference.remote.openai_compat import (
    _normalize_base_url,
    _Outcome,
    _parse_sse_line,
)
from engine.inference.types import (
    BackendNotReadyError,
    BackendState,
    GenerationFailedError,
    GenerationRequest,
    Message,
)
from tests.inference.remote.fake_openai_server import TOKENS, FakeOpenAIServer, serve

REMOTE_MODEL = "meta-llama/fake"

# Every backend behavior test runs against each concrete adapter.
ADAPTERS = [
    pytest.param(RemoteVLLMBackend, "remote-vllm", id="vllm"),
    pytest.param(RemoteSGLangBackend, "remote-sglang", id="sglang"),
]


def _backend(
    backend_cls: type[OpenAICompatibleRemoteBackend], base_url: str, **overrides: object
) -> OpenAICompatibleRemoteBackend:
    params: dict = {
        "base_url": base_url,
        "remote_model": REMOTE_MODEL,
        "model_id": "local-model",
        "max_prestream_retries": 1,
    }
    params.update(overrides)
    return backend_cls(RemoteBackendConfig(**params))


def _chat_req(**kw: object) -> GenerationRequest:
    return GenerationRequest(messages=[Message(role="user", content="hi")], **kw)  # type: ignore[arg-type]


async def _drain(stream: object) -> str:
    parts = []
    async for chunk in stream:  # type: ignore[attr-defined]
        parts.append(chunk.text)
    return "".join(parts)


# -- happy path -----------------------------------------------------------


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_chat_streaming_happy_path(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL)

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(backend_cls, url, api_key="secret-key")
            await backend.load()
            assert backend.state is BackendState.READY
            caps = backend.capabilities()
            assert caps.backend == expected_name
            assert caps.model_id == "local-model"
            assert caps.context_length == 8192  # probed from /v1/models
            assert caps.supports_structured_output is False

            stream = backend.generate(_chat_req())
            text = await _drain(stream)
            assert text == "".join(TOKENS)
            result = stream.result
            assert result is not None
            assert result.finish_reason.value == "stop"
            assert result.prompt_tokens == 7
            assert result.completion_tokens == len(TOKENS)
            await backend.unload()
            assert backend.state is BackendState.UNLOADED

    asyncio.run(body())


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_completion_streaming_happy_path(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL)

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(backend_cls, url)
            await backend.load()
            stream = backend.generate(GenerationRequest(prompt="hello"))
            text = await _drain(stream)
            assert text == "".join(TOKENS)
            assert fake.completion_calls == 1

    asyncio.run(body())


# -- explicit model mapping + credentials ---------------------------------


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_explicit_model_mapping_and_bearer_credentials(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL)

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(backend_cls, url, api_key="sk-topsecret")
            await backend.load()
            await _drain(backend.generate(_chat_req(request_id="req-123")))
            assert fake.last_body["model"] == REMOTE_MODEL
            assert fake.last_auth == "Bearer sk-topsecret"
            assert fake.last_request_id == "req-123"

    asyncio.run(body())


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_health_never_leaks_credentials(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL)

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(backend_cls, url, api_key="sk-topsecret")
            await backend.load()
            snapshot = await backend.health()
            blob = json.dumps(snapshot)
            assert "sk-topsecret" not in blob
            assert "authorization" not in blob.lower()
            assert snapshot["backend"] == expected_name
            assert snapshot["remote_model"] == REMOTE_MODEL
            assert snapshot["state"] == "ready"

    asyncio.run(body())


# -- health / readiness at load ------------------------------------------


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_load_fails_when_model_not_served(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    fake = FakeOpenAIServer(model="some-other-model")

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(backend_cls, url)
            with pytest.raises(Exception) as excinfo:
                await backend.load()
            assert "does not serve" in str(excinfo.value)
            assert backend.state is BackendState.UNLOADED

    asyncio.run(body())


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_generate_before_ready_raises(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    backend = _backend(backend_cls, "http://127.0.0.1:1")  # never loaded
    with pytest.raises(BackendNotReadyError):
        backend.generate(_chat_req())


# -- safe pre-stream failover only ---------------------------------------


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_prestream_transient_error_is_retried(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL, mode="prestream_error", fail_times=1)

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(backend_cls, url, max_prestream_retries=1)
            await backend.load()
            text = await _drain(backend.generate(_chat_req()))
            assert text == "".join(TOKENS)
            assert fake.chat_calls == 2  # one failed attempt + one success

    asyncio.run(body())


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_prestream_retries_are_bounded(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL, mode="prestream_error", fail_times=5)

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(backend_cls, url, max_prestream_retries=1)
            await backend.load()
            with pytest.raises(GenerationFailedError):
                await _drain(backend.generate(_chat_req()))
            assert fake.chat_calls == 2  # initial attempt + exactly one retry

    asyncio.run(body())


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_non_retriable_status_is_not_retried(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL, mode="http_400")

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(backend_cls, url, max_prestream_retries=3)
            await backend.load()
            with pytest.raises(GenerationFailedError):
                await _drain(backend.generate(_chat_req()))
            assert fake.chat_calls == 1  # a 4xx is a hard failure, never retried

    asyncio.run(body())


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_no_failover_after_first_token(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    # The upstream drops the connection after emitting one token. Because a token
    # has already reached the client, the backend must NOT retry: it ends the
    # stream honestly with the partial text and a single upstream attempt.
    fake = FakeOpenAIServer(model=REMOTE_MODEL, mode="drop_after_first")

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(backend_cls, url, max_prestream_retries=3)
            await backend.load()
            stream = backend.generate(_chat_req())
            received: list[str] = []
            with pytest.raises(GenerationFailedError):
                async for chunk in stream:
                    received.append(chunk.text)
            assert received == [TOKENS[0]]  # exactly the one token that got through
            assert fake.chat_calls == 1  # no silent re-route
            result = stream.result
            assert result is not None
            assert result.finish_reason.value == "error"

    asyncio.run(body())


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_aclose_before_iteration_is_clean(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL)

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(backend_cls, url)
            await backend.load()
            stream = backend.generate(_chat_req())
            await stream.aclose()
            result = stream.result
            assert result is not None
            assert result.finish_reason.value == "cancelled"

    asyncio.run(body())


# -- SSE parser + base_url normalization (fast, no server) ----------------


def test_parse_sse_line_variants() -> None:
    outcome = _Outcome()
    assert _parse_sse_line(": keepalive", is_chat=True, outcome=outcome) == []
    assert _parse_sse_line("", is_chat=True, outcome=outcome) == []
    assert _parse_sse_line("data: {bad json", is_chat=True, outcome=outcome) == []

    chunk = 'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}'
    assert _parse_sse_line(chunk, is_chat=True, outcome=outcome) == ["hi"]

    length = 'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"length"}]}'
    assert _parse_sse_line(length, is_chat=True, outcome=outcome) == ["x"]
    assert outcome.finish.value == "length"

    usage = 'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":9}}'
    assert _parse_sse_line(usage, is_chat=True, outcome=outcome) == []
    assert outcome.usage_seen is True
    assert outcome.prompt_tokens == 3

    comp = 'data: {"choices":[{"text":"yo","finish_reason":null}]}'
    assert _parse_sse_line(comp, is_chat=False, outcome=outcome) == ["yo"]

    done = _parse_sse_line("data: [DONE]", is_chat=True, outcome=outcome)
    assert len(done) == 1 and done[0] is not None


def test_normalize_base_url_accepts_both_forms() -> None:
    assert _normalize_base_url("http://h:8000") == "http://h:8000"
    assert _normalize_base_url("http://h:8000/") == "http://h:8000"
    assert _normalize_base_url("http://h:8000/v1") == "http://h:8000"
    assert _normalize_base_url("http://h:8000/v1/") == "http://h:8000"


# -- KV/prefix-cache metrics + capability gating (Block 10, sub-slice 4) ---

_VLLM_METRICS = """\
# HELP vllm:gpu_cache_usage_perc GPU KV-cache usage.
# TYPE vllm:gpu_cache_usage_perc gauge
vllm:gpu_cache_usage_perc{model_name="m"} 0.42
# TYPE vllm:prefix_cache_queries_total counter
vllm:prefix_cache_queries_total{model_name="m"} 200.0
vllm:prefix_cache_hits_total{model_name="m"} 150.0
"""

_SGLANG_METRICS = """\
sglang:token_usage{model="m"} 0.30
sglang:cache_hit_rate{model="m"} 0.80
"""


def test_parse_cache_metrics_vllm_counters() -> None:
    from engine.inference.remote.openai_compat import _parse_cache_metrics

    stats = _parse_cache_metrics(_VLLM_METRICS)
    assert stats is not None
    assert stats["kv_cache_utilization"] == 0.42
    # hit rate computed from counters: 150 / 200
    assert stats["prefix_cache_hit_rate"] == 0.75


def test_parse_cache_metrics_sglang_gauges() -> None:
    from engine.inference.remote.openai_compat import _parse_cache_metrics

    stats = _parse_cache_metrics(_SGLANG_METRICS)
    assert stats == {"kv_cache_utilization": 0.30, "prefix_cache_hit_rate": 0.80}


def test_parse_cache_metrics_none_when_absent() -> None:
    from engine.inference.remote.openai_compat import _parse_cache_metrics

    assert _parse_cache_metrics("# just comments\n") is None
    assert _parse_cache_metrics("some_other_metric 1.0\n") is None


@pytest.mark.parametrize("backend_cls,expected_name", ADAPTERS)
def test_capabilities_report_prefix_and_kv_flags(
    backend_cls: type[OpenAICompatibleRemoteBackend], expected_name: str
) -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL)

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(backend_cls, url)
            await backend.load()
            caps = backend.capabilities()
            assert caps.supports_prefix_cache is True
            assert caps.supports_kv_cache_metrics is True

            # Honest gating: flags off in config -> reported False.
            off = _backend(backend_cls, url, prefix_cache=False, kv_metrics=False)
            await off.load()
            off_caps = off.capabilities()
            assert off_caps.supports_prefix_cache is False
            assert off_caps.supports_kv_cache_metrics is False

    asyncio.run(body())


def test_cache_stats_scrapes_metrics() -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL, metrics_text=_VLLM_METRICS)

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(RemoteVLLMBackend, url)
            await backend.load()
            stats = await backend.cache_stats()
            assert stats is not None
            assert stats["kv_cache_utilization"] == 0.42
            assert stats["prefix_cache_hit_rate"] == 0.75

    asyncio.run(body())


def test_cache_stats_none_when_metrics_disabled() -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL, metrics_text=_VLLM_METRICS)

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(RemoteVLLMBackend, url, kv_metrics=False)
            await backend.load()
            assert await backend.cache_stats() is None  # gated off by config

    asyncio.run(body())


def test_cache_stats_none_when_endpoint_missing() -> None:
    fake = FakeOpenAIServer(model=REMOTE_MODEL, metrics_text=None)  # /metrics 404s

    async def body() -> None:
        with serve(fake) as url:
            backend = _backend(RemoteVLLMBackend, url)
            await backend.load()
            assert await backend.cache_stats() is None

    asyncio.run(body())
