"""Contract test: the official OpenAI Python SDK works against the engine.

Boots a real Uvicorn server (in a thread) with a ready fake backend and drives it
with the ``openai`` SDK via ``base_url`` — no custom protocol code. The same test
runs against the real model in the AVX CI job.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from engine.config import Settings
from engine.main import create_app
from tests.api.conftest import EXPECTED_TEXT, MODEL_ID, TOKENS, running_app
from tests.support.fake_backend import FakeBackend


@pytest.fixture
def base_url(tmp_path: Path) -> Iterator[str]:
    fake = FakeBackend(tokens=TOKENS, model_id=MODEL_ID)
    asyncio.run(fake.load())
    app = create_app(Settings(data_dir=tmp_path), backend=fake)
    with running_app(app) as base:
        yield f"{base}/v1"


def _client(base_url: str):  # type: ignore[no-untyped-def]
    from openai import OpenAI

    return OpenAI(base_url=base_url, api_key="sk-test")


def test_sdk_non_streaming(base_url: str) -> None:
    client = _client(base_url)
    resp = client.chat.completions.create(
        model=MODEL_ID, messages=[{"role": "user", "content": "hi there"}]
    )
    assert resp.object == "chat.completion"
    assert resp.choices[0].message.content == EXPECTED_TEXT
    assert resp.choices[0].finish_reason == "stop"
    assert resp.usage is not None and resp.usage.completion_tokens == len(TOKENS)


def test_sdk_streaming(base_url: str) -> None:
    client = _client(base_url)
    stream = client.chat.completions.create(
        model=MODEL_ID, messages=[{"role": "user", "content": "hi"}], stream=True
    )
    parts = [c.choices[0].delta.content for c in stream if c.choices[0].delta.content]
    assert "".join(parts) == EXPECTED_TEXT


def test_sdk_lists_models(base_url: str) -> None:
    client = _client(base_url)
    ids = [m.id for m in client.models.list().data]
    assert MODEL_ID in ids


def test_sdk_benign_params_are_accepted(base_url: str) -> None:
    # Drop-in tolerance: the real SDK sending an optional sampler param we do not
    # yet apply must NOT error (it is accepted and ignored).
    client = _client(base_url)
    resp = client.chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": "hi"}],
        frequency_penalty=0.5,
        user="u123",
    )
    assert resp.choices[0].message.content is not None


def test_sdk_rejects_contract_changing_features(base_url: str) -> None:
    # Features we cannot honor and must not silently drop still raise clearly.
    from openai import BadRequestError

    client = _client(base_url)
    with pytest.raises(BadRequestError):
        client.chat.completions.create(
            model=MODEL_ID,
            messages=[{"role": "user", "content": "hi"}],
            response_format={"type": "json_object"},
        )
