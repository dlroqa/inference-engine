"""Build the configured inference backend from settings.

Block 1 supported exactly one backend (llama.cpp). Block 10 adds remote adapters
(vLLM, then SGLang): ``backend_kind`` selects the runtime. Backend *registry* and
health-aware routing across several live backends are later sub-slices.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engine.config import Settings
from engine.inference.base import InferenceBackend
from engine.inference.llamacpp import LlamaCppBackend
from engine.inference.types import ModelLoadError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from engine.config import RemoteWorkerSpec


def build_backend(settings: Settings) -> InferenceBackend:
    """Construct (but do not load) the backend selected by ``backend_kind``."""
    if settings.backend_kind.startswith("remote_"):
        return _build_remote(settings)
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


def _build_remote(settings: Settings) -> InferenceBackend:
    if not settings.allow_remote_backends:
        raise ModelLoadError(
            "remote backends are disabled (allow_remote_backends=false); "
            "enable it to proxy inference to an external server"
        )
    if not settings.remote_base_url or not settings.remote_model:
        # Defensive: config validation already enforces this for remote_* kinds.
        raise ModelLoadError(
            f"{settings.backend_kind} backend requires remote_base_url and remote_model"
        )
    from engine.inference.remote import (
        RemoteBackendConfig,
        RemoteSGLangBackend,
        RemoteVLLMBackend,
    )

    classes = {
        "remote_vllm": RemoteVLLMBackend,
        "remote_sglang": RemoteSGLangBackend,
    }
    backend_cls = classes[settings.backend_kind]
    return backend_cls(
        RemoteBackendConfig(
            base_url=settings.remote_base_url,
            remote_model=settings.remote_model,
            model_id=settings.model_id,
            api_key=settings.remote_api_key,
            connect_timeout_s=settings.remote_connect_timeout_s,
            read_timeout_s=settings.remote_read_timeout_s,
            max_prestream_retries=settings.remote_max_prestream_retries,
            context_length=settings.remote_context_length,
            tls_verify=settings.remote_tls_verify,
            prefix_cache=settings.remote_prefix_cache,
            kv_metrics=settings.remote_kv_metrics,
        )
    )


def build_remote_worker(spec: RemoteWorkerSpec) -> InferenceBackend:
    """Construct (but do not load) a remote backend from a ``remote_workers`` spec."""
    from engine.inference.remote import (
        RemoteBackendConfig,
        RemoteSGLangBackend,
        RemoteVLLMBackend,
    )

    classes = {"remote_vllm": RemoteVLLMBackend, "remote_sglang": RemoteSGLangBackend}
    backend_cls = classes[spec.kind]
    return backend_cls(
        RemoteBackendConfig(
            base_url=spec.base_url,
            remote_model=spec.model,
            model_id="local-model",  # the pool serves the engine's single model_id
            api_key=spec.api_key,
            connect_timeout_s=spec.connect_timeout_s,
            read_timeout_s=spec.read_timeout_s,
            max_prestream_retries=spec.max_prestream_retries,
            context_length=spec.context_length,
            tls_verify=spec.tls_verify,
            prefix_cache=spec.prefix_cache,
            kv_metrics=spec.kv_metrics,
        )
    )
