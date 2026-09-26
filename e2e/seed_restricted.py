"""Seed the restricted engine's data directory before its switches are turned off.

Run once against the engine started with model management **on** (CI step
"Run the browser acceptance suite"). It imports a copy of the checksum-pinned
fixture under the name ``tiny-ready`` and waits until the registry reports it
ready. The engine is then restarted with ``allow_model_management`` and
``allow_network_downloads`` off and with the fixture configured as its loaded
model, so the restricted dashboard shows a loaded row (Unload) and a ready row
(Load, Delete) whose actions must be disabled.

Environment (all required; nothing is printed except status codes and states):

- ``IE_SEED_BASE_URL``
- ``IE_SEED_OPERATOR_KEY``
- ``IE_SEED_MODEL_PATH``  the fixture copy to import
"""

from __future__ import annotations

import os
import sys
import time

import httpx

READY_NAME = "tiny-ready"


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"{name} must be set")
    return value


def main() -> int:
    base = _env("IE_SEED_BASE_URL").rstrip("/")
    key = _env("IE_SEED_OPERATOR_KEY")
    path = _env("IE_SEED_MODEL_PATH")
    headers = {"authorization": f"Bearer {key}"}
    with httpx.Client(base_url=base, headers=headers, timeout=120.0) as c:
        resp = c.post("/admin/models/import", json={"path": path, "name": READY_NAME})
        print(f"import: HTTP {resp.status_code}")
        if resp.status_code not in (200, 201, 202):
            return 1
        model_id = resp.json()["id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = c.get(f"/admin/models/{model_id}").json()["status"]
            if status == "ready":
                print("seeded: tiny-ready is ready")
                return 0
            if status in ("error", "cancelled"):
                print(f"seed failed: status {status}")
                return 1
            time.sleep(0.5)
        print("seed failed: not ready within 120 s")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
