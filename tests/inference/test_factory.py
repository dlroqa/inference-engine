"""Backend factory wiring from settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import Settings
from engine.inference import build_backend
from engine.inference.llamacpp import LlamaCppBackend
from engine.inference.types import ModelLoadError


def test_build_backend_requires_model_path(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    with pytest.raises(ModelLoadError):
        build_backend(settings)


def test_build_backend_constructs_llamacpp(tmp_path: Path) -> None:
    model = tmp_path / "model.gguf"
    model.write_bytes(b"not a real model")
    settings = Settings(data_dir=tmp_path, model_path=model, model_id="m1", n_ctx=1024)
    backend = build_backend(settings)
    assert isinstance(backend, LlamaCppBackend)
    assert backend.state.value == "unloaded"
