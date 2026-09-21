"""Structured output (Block 8): capability-gated JSON/grammar constraints.

The default fake backend reports no structured-output support, so these use a
fake backend that does; schema *conformance* against a real grammar is verified
by the AVX2 integration job (see scripts/structured_output_smoke.py).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app
from tests.api.conftest import MODEL_ID
from tests.support.fake_backend import FakeBackend


@pytest.fixture
def structured_backend() -> FakeBackend:
    fake = FakeBackend(tokens=["{}"], model_id=MODEL_ID, supports_structured_output=True)
    asyncio.run(fake.load())
    return fake


@pytest.fixture
def structured_client(
    tmp_settings: Settings, structured_backend: FakeBackend
) -> Iterator[tuple[TestClient, FakeBackend]]:
    app = create_app(tmp_settings, backend=structured_backend)
    with TestClient(app) as c:
        yield c, structured_backend


def test_json_object_passes_through(structured_client: tuple[TestClient, FakeBackend]) -> None:
    client, fake = structured_client
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert fake.last_request is not None
    assert fake.last_request.json_object is True
    assert fake.last_request.json_schema is None


def test_json_schema_passes_schema_through(
    structured_client: tuple[TestClient, FakeBackend],
) -> None:
    client, fake = structured_client
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_schema", "json_schema": {"schema": schema}},
        },
    )
    assert resp.status_code == 200, resp.text
    assert fake.last_request is not None
    assert fake.last_request.json_schema == schema


def test_json_schema_without_schema_rejected(
    structured_client: tuple[TestClient, FakeBackend],
) -> None:
    client, _ = structured_client
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_schema", "json_schema": {}},
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "response_format"


def test_unsupported_backend_rejects_structured(tmp_settings: Settings) -> None:
    fake = FakeBackend(tokens=["x"], model_id=MODEL_ID, supports_structured_output=False)
    asyncio.run(fake.load())
    with TestClient(create_app(tmp_settings, backend=fake)) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_object"},
            },
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "structured_output_unsupported"
