"""App lifecycle: it starts even when the configured model fails to load."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from engine.config import Settings
from engine.main import create_app


def test_app_starts_when_model_load_fails(tmp_path: Path) -> None:
    # model_path is set but llama-cpp-python isn't installed here, so the startup
    # load fails; the app must still start and report inference unavailable.
    model = tmp_path / "model.gguf"
    model.write_bytes(b"not a real model")
    settings = Settings(data_dir=tmp_path, model_path=model)

    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/models").json()["data"] == []
        body = client.get("/readyz").json()
        assert body["status"] == "ready"  # service ready; model just absent
        assert body["inference"]["available"] is False
