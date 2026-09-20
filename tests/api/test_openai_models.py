"""GET /v1/models."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.conftest import MODEL_ID


def test_lists_loaded_model(client: TestClient) -> None:
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert ids == [MODEL_ID]
    assert body["data"][0]["object"] == "model"


def test_empty_when_no_model(client_no_model: TestClient) -> None:
    body = client_no_model.get("/v1/models").json()
    assert body["data"] == []
