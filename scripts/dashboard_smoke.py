"""Manual smoke test for the operator dashboard's control plane (Block 5).

Drives a running engine through the same admin/operability API the dashboard uses
and asserts the operator flow end to end:

  1. model load           (POST /admin/model/load)
  2. a generation         (POST /v1/chat/completions)
  3. metric update         (GET /admin/overview — request counter increments)
  4. log inspection        (GET /logs)
  5. key create + revoke   (POST/DELETE /admin/keys)
  6. system contract       (GET /admin/system — shape, types, and readiness)

Usage:
    python scripts/dashboard_smoke.py --base-url http://127.0.0.1:8000 [--api-key sk-ie-...]

Two explicit modes:

- default (real model): the configured model **must** load and generate, and
  ``/admin/system`` must report inference available. There is no silent
  fallback: a model that fails to load fails the smoke.
- ``--skip-generation`` (no model, e.g. the installed-package smoke): generation
  is not attempted and inference availability is not required, but every other
  check — including the ``/admin/system`` contract — still runs.

Exits non-zero if any check fails. Output lists check names and status codes
only — never raw responses, credentials, prompts, or generated text.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

SYSTEM_KEYS = {"build", "readiness", "draining", "switches", "metadata", "routing"}
SYSTEM_SWITCHES = {
    "allow_model_management",
    "allow_network_downloads",
    "allow_structured_output",
    "diagnostics_enabled",
    "require_auth",
    "webhooks_enabled",
    "client_events_enabled",
    "ip_allowlist_set",
    "grpc_enabled",
}


def _is_str_or_none(value: object) -> bool:
    return value is None or isinstance(value, str)


def validate_system(body: Any, *, require_inference: bool) -> list[str]:
    """Problems with a ``GET /admin/system`` body (empty list = valid).

    Mirrors the contract in engine/api/admin_router.py::system_summary: exact
    top-level keys, real booleans (never truthy strings), nullable typed
    metadata, and — in real-model mode — inference reported available.
    """
    if not isinstance(body, dict):
        return ["body is not an object"]
    problems: list[str] = []
    if set(body) != SYSTEM_KEYS:
        problems.append(f"top-level keys {sorted(body)} != {sorted(SYSTEM_KEYS)}")
        return problems

    build = body["build"]
    if not isinstance(build, dict) or not {"version", "commit", "built_at"} <= set(build):
        problems.append("build lacks version/commit/built_at")
    elif not isinstance(build["version"], str) or not build["version"]:
        problems.append("build.version is not a non-empty string")
    elif not (_is_str_or_none(build["commit"]) and _is_str_or_none(build["built_at"])):
        problems.append("build.commit/built_at are not string-or-null")

    readiness = body["readiness"]
    if not isinstance(readiness, dict) or not {"ready", "checks", "inference"} <= set(readiness):
        problems.append("readiness lacks ready/checks/inference")
    else:
        if not isinstance(readiness["ready"], bool):
            problems.append("readiness.ready is not a boolean")
        checks = readiness["checks"]
        if not isinstance(checks, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in checks.items()
        ):
            problems.append("readiness.checks is not a string map")
        elif checks.get("database") != "ok" or checks.get("migrations") != "applied":
            problems.append("database/migrations not healthy")
        inference = readiness["inference"]
        if not isinstance(inference, dict) or not isinstance(inference.get("available"), bool):
            problems.append("readiness.inference.available is not a boolean")
        elif require_inference and inference["available"] is not True:
            problems.append("inference not available after model load")

    if not isinstance(body["draining"], bool):
        problems.append("draining is not a boolean")
    elif body["draining"]:
        problems.append("engine reports draining during the smoke")

    switches = body["switches"]
    if not isinstance(switches, dict) or set(switches) != SYSTEM_SWITCHES:
        problems.append("switches do not match the agreed set")
    elif not all(isinstance(v, bool) for v in switches.values()):
        problems.append("a switch is not a real boolean")

    metadata = body["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != {"grpc_port", "billing_provider"}:
        problems.append("metadata keys are not grpc_port/billing_provider")
    else:
        port = metadata["grpc_port"]
        if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
            problems.append("metadata.grpc_port is not int-or-null")
        if not _is_str_or_none(metadata["billing_provider"]):
            problems.append("metadata.billing_provider is not string-or-null")
        grpc_off = isinstance(switches, dict) and switches.get("grpc_enabled") is False
        if grpc_off and port is not None:
            problems.append("grpc_port set while gRPC is disabled")

    routing = body["routing"]
    expected_routing = {
        "backend_kind",
        "virtual_models",
        "workload_routing_enabled",
        "workload_rule_count",
        "remote_workers",
        "spillover_providers",
    }
    if not isinstance(routing, dict) or set(routing) != expected_routing:
        problems.append("routing keys do not match the agreed summary")
    else:
        if not isinstance(routing["backend_kind"], str):
            problems.append("routing.backend_kind is not a string")
        if not isinstance(routing["workload_routing_enabled"], bool):
            problems.append("routing.workload_routing_enabled is not a boolean")
        count = routing["workload_rule_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            problems.append("routing.workload_rule_count is not a non-negative int")
        for name in ("remote_workers", "spillover_providers"):
            names = routing[name]
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                problems.append(f"routing.{name} is not a list of names")
        vms = routing["virtual_models"]
        if not isinstance(vms, list) or not all(
            isinstance(v, dict) and set(v) == {"name", "policy"} for v in vms
        ):
            problems.append("routing.virtual_models entries are not {name, policy}")
    return problems


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
        real_model = not skip_generation

        # 1. Model load (idempotent). In real-model mode the configured model must
        # be loaded afterwards; with --skip-generation any safe outcome is fine.
        status, load = self._req("POST", "/admin/model/load")
        loaded = status == 200 and bool(load.get("model", {}).get("loaded"))
        if real_model:
            self.check("model loaded (real-model mode)", loaded, f"status={status}")
        else:
            self.check("model load endpoint", status in (200, 400, 503), f"status={status}")

        # 3a. Baseline metrics.
        status, ov0 = self._req("GET", "/admin/overview")
        self.check("overview before", status == 200, f"status={status}")
        before = ov0.get("metrics", {}).get("counters", {}).get("requests_total", 0)

        # 2. A generation: required in real-model mode, never silently skipped.
        if real_model:
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
            choices = gen.get("choices") if isinstance(gen, dict) else None
            message = choices[0].get("message", {}) if choices else {}
            produced = isinstance(message.get("content"), str)
            self.check("generation", status == 200 and produced, f"status={status}")
        else:
            print("[SKIP] generation — explicit --skip-generation (no model configured)")

        # 3b. Metric update: overview counter should reflect activity.
        status, ov1 = self._req("GET", "/admin/overview")
        after = ov1.get("metrics", {}).get("counters", {}).get("requests_total", 0)
        if real_model:
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

        # 6. System contract (read-only; the dashboard's switch/readiness source).
        status, system = self._req("GET", "/admin/system")
        self.check("system endpoint", status == 200, f"status={status}")
        if status == 200:
            problems = validate_system(system, require_inference=real_model)
            self.check(
                "system contract"
                + (" + inference available" if real_model else " (no-model mode)"),
                not problems,
                "; ".join(problems),
            )

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
        "--skip-generation",
        action="store_true",
        help="No model is configured: skip generation and do not require inference.",
    )
    args = parser.parse_args()
    return Smoke(args.base_url, args.api_key).run(skip_generation=args.skip_generation)


if __name__ == "__main__":
    sys.exit(main())
