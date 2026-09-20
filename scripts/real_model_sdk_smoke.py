"""CI smoke: drive a running engine (with a real model) via the OpenAI SDK.

Waits for readiness, then exercises models.list + non-streaming and streaming
chat completions. Used by the AVX ``integration-llama`` CI job. Base URL and model
id come from env (defaults suit the CI job).
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
    from openai import OpenAI

    _wait_ready()
    client = OpenAI(base_url=f"{BASE}/v1", api_key="sk-test")

    ids = [m.id for m in client.models.list().data]
    assert MODEL in ids, f"{MODEL} not in {ids}"

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "The capital of France is"}],
        max_tokens=16,
    )
    text = resp.choices[0].message.content
    print("NON-STREAM:", repr(text))
    assert text, "empty non-streaming completion"

    parts = []
    for chunk in client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Say hello"}],
        max_tokens=16,
        stream=True,
    ):
        delta = chunk.choices[0].delta.content
        if delta:
            parts.append(delta)
    print("STREAM:", repr("".join(parts)))
    assert parts, "empty streaming completion"

    print("OpenAI SDK against the real model: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
