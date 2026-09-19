"""App factory creates an application successfully (Block 0 required test)."""

from __future__ import annotations

from fastapi import FastAPI

from engine.config import Settings
from engine.main import create_app, get_settings


def test_create_app_returns_fastapi(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings)
    assert isinstance(app, FastAPI)
    assert app.title == "Inference Engine"


def test_create_app_sets_active_settings(tmp_settings: Settings) -> None:
    create_app(tmp_settings)
    assert get_settings() is tmp_settings


def test_health_routes_registered(tmp_settings: Settings) -> None:
    app = create_app(tmp_settings)
    # Use the public OpenAPI schema rather than private route internals.
    paths = set(app.openapi()["paths"])
    assert "/healthz" in paths
    assert "/readyz" in paths
