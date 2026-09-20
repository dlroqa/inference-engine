"""Inference runtime: the internal generation contract and backends (Block 1).

Public surface:

- Types: :class:`GenerationRequest`, :class:`GenerationChunk`,
  :class:`GenerationResult`, :class:`FinishReason`, :class:`BackendState`,
  :class:`Capabilities`, and the backend error taxonomy.
- Interfaces: :class:`InferenceBackend`, :class:`GenerationStream`.
- Concrete: :class:`LlamaCppBackend` (optional ``llama`` extra) and
  :func:`build_backend`.
"""

from engine.inference.base import GenerationStream, InferenceBackend
from engine.inference.factory import build_backend
from engine.inference.threaded import ThreadedBackend
from engine.inference.types import (
    BackendBusyError,
    BackendError,
    BackendNotReadyError,
    BackendState,
    Capabilities,
    FinishReason,
    GenerationChunk,
    GenerationFailedError,
    GenerationRequest,
    GenerationResult,
    Message,
    ModelLoadError,
)

__all__ = [
    "BackendBusyError",
    "BackendError",
    "BackendNotReadyError",
    "BackendState",
    "Capabilities",
    "FinishReason",
    "GenerationChunk",
    "GenerationFailedError",
    "GenerationRequest",
    "GenerationResult",
    "GenerationStream",
    "InferenceBackend",
    "Message",
    "ModelLoadError",
    "ThreadedBackend",
    "build_backend",
]
