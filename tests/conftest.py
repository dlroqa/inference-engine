"""Shared test fixtures.

Every test that needs configuration uses an isolated temporary ``data_dir`` so
no test touches per-user OS paths or the real database.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from engine.config import Settings


@pytest.fixture(autouse=True)
def _clean_ie_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any ambient IE_* variables so tests are deterministic."""
    for key in list(os.environ):
        if key.startswith("IE_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(data_dir=data_dir)


@pytest.fixture
def client(tmp_settings: Settings) -> Iterator[object]:
    from fastapi.testclient import TestClient

    from engine.main import create_app

    app = create_app(tmp_settings)
    with TestClient(app) as test_client:  # triggers lifespan (migrations)
        yield test_client
