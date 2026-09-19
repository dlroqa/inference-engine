"""Health endpoints return their documented responses (Block 0 required)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_readyz_ready_after_migrations(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["migrations"] == "applied"


def test_readyz_does_not_imply_inference_available(client: TestClient) -> None:
    body = client.get("/readyz").json()
    assert body["inference"]["available"] is False
    assert body["inference"]["reason"]
