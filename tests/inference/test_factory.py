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


def test_build_backend_constructs_remote_vllm(tmp_path: Path) -> None:
    from engine.inference.remote.vllm import RemoteVLLMBackend

    settings = Settings(
        data_dir=tmp_path,
        backend_kind="remote_vllm",
        remote_base_url="http://vllm.internal:8000",
        remote_model="meta-llama/x",
        model_id="served-x",
    )
    backend = build_backend(settings)
    assert isinstance(backend, RemoteVLLMBackend)
    assert backend.state.value == "unloaded"


def test_remote_vllm_requires_base_url_and_model(tmp_path: Path) -> None:
    from engine.config import ConfigError, load_config

    with pytest.raises(ConfigError) as excinfo:
        load_config({"data_dir": tmp_path, "backend_kind": "remote_vllm"})
    assert "remote_base_url" in str(excinfo.value)


def test_remote_backend_kill_switch(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        backend_kind="remote_vllm",
        remote_base_url="http://vllm.internal:8000",
        remote_model="meta-llama/x",
        allow_remote_backends=False,
    )
    with pytest.raises(ModelLoadError) as excinfo:
        build_backend(settings)
    assert "disabled" in str(excinfo.value)
