"""Remote vLLM backend — the first Block 10 remote adapter.

vLLM exposes an OpenAI-compatible server, so this adapter is a thin subclass of
:class:`~engine.inference.remote.openai_compat.OpenAICompatibleRemoteBackend`,
which owns the streaming, retry (safe pre-stream failover only), health, and
credential handling. See docs/backends.md.
"""

from __future__ import annotations

from engine.inference.remote.openai_compat import (
    OpenAICompatibleRemoteBackend,
    RemoteBackendConfig,
)

#: Backwards-compatible alias — the config is shared across remote adapters.
RemoteVLLMConfig = RemoteBackendConfig


class RemoteVLLMBackend(OpenAICompatibleRemoteBackend):
    """A single configured remote vLLM model, served over its OpenAI API."""

    name = "remote-vllm"
