"""Build the configured inference backend from settings.

Block 1 supports exactly one backend (llama.cpp) and one explicitly configured
model. Backend selection/registry and remote workers are later blocks.
"""

from __future__ import annotations

from engine.config import Settings
from engine.inference.base import InferenceBackend
from engine.inference.llamacpp import LlamaCppBackend
from engine.inference.types import ModelLoadError


def build_backend(settings: Settings) -> InferenceBackend:
    """Construct (but do not load) the backend for the configured model."""
    if settings.model_path is None:
        raise ModelLoadError(
            "no model configured; set model_path (IE_MODEL_PATH, --model-path, "
            "or model_path in config.toml)"
        )
    return LlamaCppBackend(
        model_path=settings.model_path,
        model_id=settings.model_id,
        n_ctx=settings.n_ctx,
        n_threads=settings.n_threads,
        n_gpu_layers=settings.n_gpu_layers,
    )
