"""CI smoke: verify constrained JSON output against a real model.

Requests ``response_format: json_schema`` (via the OpenAI SDK) and asserts the
returned text parses as JSON and satisfies the required keys. Proves the engine's
structured-output path actually constrains decoding on the installed llama.cpp
binding — the exit-gate claim for Block 8. Used by the AVX ``integration-llama``
CI job.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("IE_SMOKE_BASE", "http://127.0.0.1:8123")
MODEL = os.environ.get("IE_MODEL_ID", "tiny")

SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
    "required": ["city", "country"],
    "additionalProperties": False,
}


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

    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "user", "content": "Return the city and country of the Eiffel Tower as JSON."}
        ],
        max_tokens=64,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "location", "schema": SCHEMA},
        },
    )
    text = resp.choices[0].message.content or ""
    print("STRUCTURED:", repr(text))
    parsed = json.loads(text)  # must be valid JSON (constrained decoding)
    assert "city" in parsed and "country" in parsed, f"schema keys missing: {parsed}"

    print("Structured output (json_schema) against the real model: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
