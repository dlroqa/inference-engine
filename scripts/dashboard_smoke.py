"""Manual smoke test for the operator dashboard's control plane (Block 5).

Drives a running engine through the same admin/operability API the dashboard uses
and asserts the operator flow end to end:

  1. model load           (POST /admin/model/load)
  2. a generation         (POST /v1/chat/completions)
  3. metric update         (GET /admin/overview — request counter increments)
  4. log inspection        (GET /logs)
  5. key create + revoke   (POST/DELETE /admin/keys)

Usage:
    python scripts/dashboard_smoke.py --base-url http://127.0.0.1:8000 [--api-key sk-ie-...]

Exits non-zero on the first failed check. A real generation needs a model
configured (IE_MODEL_PATH / config); without one, pass --skip-generation to still
exercise load-handling, metrics, logs, and keys. Intended for a real environment
(e.g. CI with the AVX model) — the local no-AVX sandbox cannot run llama.cpp.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


class Smoke:
    def __init__(self, base_url: str, api_key: str | None) -> None:
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.failures: list[str] = []

    def _req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.load(exc)
            except Exception:
                return exc.code, {}

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}{f' — {detail}' if detail else ''}")
        if not ok:
            self.failures.append(name)

    def wait_ready(self, timeout_s: float = 30.0) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            status, _ = self._req("GET", "/healthz")
            if status == 200:
                return
            time.sleep(0.5)
        self.check("server reachable", False, "healthz never returned 200")

    def run(self, *, skip_generation: bool) -> int:
        self.wait_ready()

        # 1. Model load (idempotent; ok if already loaded or not configured).
        status, load = self._req("POST", "/admin/model/load")
        self.check(
            "model load endpoint",
            status in (200, 400, 503),
            f"status={status} result={load.get('result') or load.get('error', {}).get('code')}",
        )
        model_available = status == 200 and load.get("model", {}).get("loaded")

        # 3a. Baseline metrics.
        status, ov0 = self._req("GET", "/admin/overview")
        self.check("overview before", status == 200, f"status={status}")
        before = ov0.get("metrics", {}).get("counters", {}).get("requests_total", 0)

        # 2. A generation (only if a model is actually loaded).
        if model_available and not skip_generation:
            model_id = ov0.get("model", {}).get("model_id") or ov0.get("model", {}).get(
                "configured_id"
            )
            status, gen = self._req(
                "POST",
                "/v1/chat/completions",
                {
                    "model": model_id,
                    "messages": [{"role": "user", "content": "Say hello"}],
                    "max_tokens": 16,
                },
            )
            self.check("generation", status == 200, f"status={status}")
        else:
            print("[SKIP] generation — no model loaded (configure a model to exercise this)")

        # 3b. Metric update: overview counter should reflect activity.
        status, ov1 = self._req("GET", "/admin/overview")
        after = ov1.get("metrics", {}).get("counters", {}).get("requests_total", 0)
        if model_available and not skip_generation:
            self.check("metrics updated", after > before, f"{before} -> {after}")
        else:
            self.check("metrics readable", status == 200 and "counters" in ov1.get("metrics", {}))

        # 4. Log inspection.
        status, logs = self._req("GET", "/logs?limit=20")
        self.check("logs readable", status == 200 and "events" in logs, f"status={status}")

        # 5. Key create + revoke lifecycle.
        status, created = self._req("POST", "/admin/keys", {"label": "dashboard-smoke"})
        created_ok = status == 200 and created.get("token", "").startswith("sk-ie-")
        self.check("key created (token shown once)", created_ok)
        if created_ok:
            key_id = created["id"]
            status, _ = self._req("DELETE", f"/admin/keys/{key_id}")
            self.check("key revoked", status == 200)
            status, listed = self._req("GET", "/admin/keys")
            revoked = any(k["id"] == key_id and k["revoked"] for k in listed.get("keys", []))
            self.check("key shows revoked in list", revoked)

        print()
        if self.failures:
            print(f"SMOKE FAILED: {len(self.failures)} check(s) failed: {', '.join(self.failures)}")
            return 1
        print("SMOKE PASSED")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Operator dashboard control-plane smoke test.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--api-key", default=None, help="Operator API key (needed if network-bound)."
    )
    parser.add_argument(
        "--skip-generation", action="store_true", help="Skip the live generation step."
    )
    args = parser.parse_args()
    return Smoke(args.base_url, args.api_key).run(skip_generation=args.skip_generation)


if __name__ == "__main__":
    sys.exit(main())
