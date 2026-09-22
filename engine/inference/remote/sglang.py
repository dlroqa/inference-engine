"""Remote SGLang backend — the second Block 10 remote adapter (sub-slice 2).

SGLang serves the same OpenAI-compatible surface as vLLM (``/v1/models``,
``/v1/chat/completions``, ``/v1/completions``, SSE deltas, and a terminal usage
chunk via ``stream_options``), so this adapter is a thin subclass of
:class:`~engine.inference.remote.openai_compat.OpenAICompatibleRemoteBackend`.
The shared base owns streaming, bounded pre-stream-only failover, health,
timeouts, and credential handling; only the adapter name differs. If a genuine
SGLang-specific request quirk ever appears, override ``_augment_body`` here — the
core does not change. See docs/backends.md.
"""

from __future__ import annotations

from engine.inference.remote.openai_compat import (
    OpenAICompatibleRemoteBackend,
    RemoteBackendConfig,
)

#: Alias for symmetry with the vLLM adapter; the config is shared.
RemoteSGLangConfig = RemoteBackendConfig


class RemoteSGLangBackend(OpenAICompatibleRemoteBackend):
    """A single configured remote SGLang model, served over its OpenAI API."""

    name = "remote-sglang"
