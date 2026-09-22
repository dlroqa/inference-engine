"""Remote inference backends (Block 10).

Remote adapters talk to an external inference server over HTTP and adapt its
streaming responses onto the same internal ``GenerationStream`` the local
llama.cpp backend produces, so nothing in the API edge, scheduler, or serving
lifecycle changes. All adapters share
:class:`~engine.inference.remote.openai_compat.OpenAICompatibleRemoteBackend`.

* :class:`~engine.inference.remote.vllm.RemoteVLLMBackend` (sub-slice 1)
* :class:`~engine.inference.remote.sglang.RemoteSGLangBackend` (sub-slice 2)
"""

from __future__ import annotations

from engine.inference.remote.openai_compat import (
    OpenAICompatibleRemoteBackend,
    RemoteBackendConfig,
)
from engine.inference.remote.sglang import RemoteSGLangBackend
from engine.inference.remote.vllm import RemoteVLLMBackend

__all__ = [
    "OpenAICompatibleRemoteBackend",
    "RemoteBackendConfig",
    "RemoteSGLangBackend",
    "RemoteVLLMBackend",
]
