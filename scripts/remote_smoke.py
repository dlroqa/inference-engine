"""CI/operator smoke: drive a remote backend against a real OpenAI-compatible server.

Exercises the remote adapter (vLLM or SGLang) end to end over real HTTP: it
verifies the configured model is served (``/v1/models``), streams a chat
completion, and asserts non-empty output with a clean terminal result.

Both adapters speak the OpenAI streaming protocol, so any conformant server
validates the adapter path. In CI we point it at a llama.cpp OpenAI server (a real
model server on a CPU runner); operators with a GPU point it straight at their
vLLM or SGLang deployment — same script, same assertions::

    python scripts/remote_smoke.py --backend sglang \
        --base-url http://sglang.internal:30000/v1 \
        --model meta-llama/Meta-Llama-3-8B-Instruct \
        --api-key "$REMOTE_API_KEY" --wait
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request

from engine.inference.remote import (
    RemoteBackendConfig,
    RemoteSGLangBackend,
    RemoteVLLMBackend,
)
from engine.inference.types import GenerationRequest, Message

_BACKENDS = {"vllm": RemoteVLLMBackend, "sglang": RemoteSGLangBackend}


def _wait_models(base_url: str, timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    url = base_url.rstrip("/") + "/models"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit(f"remote server at {url} did not become reachable in time")


async def _run(args: argparse.Namespace) -> int:
    backend = _BACKENDS[args.backend](
        RemoteBackendConfig(
            base_url=args.base_url,
            remote_model=args.model,
            model_id="smoke-model",
            api_key=args.api_key,
            read_timeout_s=args.read_timeout,
        )
    )
    await backend.load()
    print(f"loaded ({args.backend}): {json.dumps(await backend.health())}")

    request = GenerationRequest(
        messages=[Message(role="user", content=args.prompt)],
        max_tokens=args.max_tokens,
        temperature=0.0,
    )
    stream = backend.generate(request)
    pieces: list[str] = []
    async for chunk in stream:
        pieces.append(chunk.text)
        print(chunk.text, end="", flush=True)
    print()
    text = "".join(pieces)
    result = stream.result
    await backend.unload()

    if not text.strip():
        print("SMOKE FAIL: empty completion", file=sys.stderr)
        return 1
    assert result is not None
    print(
        "SMOKE OK: "
        + json.dumps(
            {
                "backend": args.backend,
                "finish_reason": result.finish_reason.value,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "chars": len(text),
            }
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=sorted(_BACKENDS), default="vllm")
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible base URL (…/v1)")
    parser.add_argument("--model", required=True, help="model id the server serves")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--prompt", default="The capital of France is")
    parser.add_argument("--max-tokens", type=int, default=24)
    parser.add_argument("--read-timeout", type=float, default=60.0)
    parser.add_argument("--wait", action="store_true", help="wait for /v1/models first")
    args = parser.parse_args()
    if args.wait:
        _wait_models(args.base_url)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
