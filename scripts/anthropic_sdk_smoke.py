"""CI smoke: drive a running engine (with a real model) via the Anthropic SDK.

Waits for readiness, then exercises non-streaming and streaming ``messages`` and
asserts the documented streaming event sequence. Used by the AVX
``integration-llama`` CI job. Base URL and model id come from env.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("IE_SMOKE_BASE", "http://127.0.0.1:8123")
MODEL = os.environ.get("IE_MODEL_ID", "tiny")


def _wait_ready(timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/readyz", timeout=2) as resp:
                data = json.load(resp)
            if data.get("inference", {}).get("available"):
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit("engine did not become ready with a loaded model in time")


def main() -> int:
    from anthropic import Anthropic

    _wait_ready()
    client = Anthropic(base_url=BASE, api_key="sk-test")

    msg = client.messages.create(
        model=MODEL,
        max_tokens=16,
        messages=[{"role": "user", "content": "The capital of France is"}],
    )
    text = "".join(block.text for block in msg.content if block.type == "text")
    print("NON-STREAM:", repr(text), "stop:", msg.stop_reason)
    assert text, "empty non-streaming message"
    assert msg.usage.output_tokens > 0

    # Streaming: collect event types and the streamed text.
    types_seen: list[str] = []
    parts: list[str] = []
    with client.messages.stream(
        model=MODEL,
        max_tokens=16,
        messages=[{"role": "user", "content": "Say hello"}],
    ) as stream:
        for event in stream:
            types_seen.append(event.type)
            if event.type == "content_block_delta" and event.delta.type == "text_delta":
                parts.append(event.delta.text)
    print("STREAM types:", types_seen)
    print("STREAM text:", repr("".join(parts)))
    assert types_seen[0] == "message_start"
    assert "content_block_delta" in types_seen
    assert types_seen[-1] == "message_stop"
    assert parts, "empty streaming message"

    print("Anthropic SDK against the real model: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
