"""Real-environment smoke for the Block 6 model lifecycle.

Against a running engine (with llama.cpp available), drives the registry API the
dashboard uses: import a GGUF, verify it becomes ready with a checksum + probed
metadata, load it, generate through it, then unload and delete. Proves the
import→load→generate→unload path end to end on a real model.

Usage:
    python scripts/model_lifecycle_smoke.py --base-url http://127.0.0.1:8000 \
        --model-path ~/models/tiny.gguf
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _req(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except Exception:
            return exc.code, {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--name", default="smoke-model")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{f' — {detail}' if detail else ''}")
        if not ok:
            failures.append(name)

    # Wait for the server.
    for _ in range(60):
        if _req(base, "GET", "/healthz")[0] == 200:
            break
        time.sleep(0.5)

    status, imported = _req(
        base, "POST", "/admin/models/import", {"path": args.model_path, "name": args.name}
    )
    check("import", status == 200 and imported.get("status") == "ready", f"status={status}")
    check("checksum recorded", bool(imported.get("sha256")))
    check("metadata probed", imported.get("arch") is not None, f"arch={imported.get('arch')}")
    model_id = imported.get("id")
    if not model_id:
        print("SMOKE FAILED: import did not return an id")
        return 1

    status, loaded = _req(base, "POST", f"/admin/models/{model_id}/load")
    check("load", status == 200 and loaded.get("model", {}).get("loaded"), f"status={status}")

    status, gen = _req(
        base,
        "POST",
        "/v1/chat/completions",
        {"model": args.name, "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 16},
    )
    text = (gen.get("choices") or [{}])[0].get("message", {}).get("content")
    check("generation through loaded model", status == 200 and bool(text), f"text={text!r}")

    status, _ = _req(base, "POST", f"/admin/models/{model_id}/unload")
    check("unload", status == 200)

    status, _ = _req(base, "DELETE", f"/admin/models/{model_id}")
    check("delete", status == 200)

    print()
    if failures:
        print(f"SMOKE FAILED: {', '.join(failures)}")
        return 1
    print("MODEL LIFECYCLE SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
