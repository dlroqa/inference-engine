"""Remote inference backends (Block 10).

The first remote adapter is vLLM's OpenAI-compatible server
(:class:`~engine.inference.remote.vllm.RemoteVLLMBackend`). Remote backends talk
to an external inference server over HTTP and adapt its streaming responses onto
the same internal ``GenerationStream`` the local llama.cpp backend produces, so
nothing in the API edge, scheduler, or serving lifecycle changes.
"""

from __future__ import annotations

from engine.inference.remote.vllm import RemoteVLLMBackend

__all__ = ["RemoteVLLMBackend"]
