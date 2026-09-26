"""Candidate-image tests for store ownership and interrupted-download recovery.

Runs the exact candidate image with plain ``docker run`` and named volumes
(stdlib only; needs Docker). Test-only orchestration: nothing here is a
production endpoint or setting.

1. Shared model volume: two engine containers with different database volumes
   and one shared model volume (mounted at different paths) conflict; the
   second refuses to start. After the owner is killed (SIGKILL), the second
   starts; the killed owner, restarted, now refuses.
2. Interrupted downloads, before and after the final rename: the repository's
   test child (``tests/support/download_child.py``, mounted read-only, using the
   image's installed engine) runs a real download to a barrier on a persistent
   volume and is killed. While it lives, an engine on that volume refuses to
   start. Afterwards the engine starts, marks the model ``error`` ("download
   interrupted") and keeps the files as found.

Usage:
    python scripts/release_image_ownership.py --image <candidate ref>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CHILD_DIR = ROOT / "tests" / "support"
LOCK_REFUSAL = "owns this model directory"
INTERRUPTED_PREFIX = "download interrupted"


class Failure(Exception):
    pass


def docker(
    *args: str, check: bool = True, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise Failure(f"docker {args[0]} failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
    return proc


def http_json(port: int, path: str, key: str | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310 (loopback)
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


class Run:
    def __init__(self, image: str) -> None:
        self.image = image
        self.tag = uuid.uuid4().hex[:8]
        self.containers: list[str] = []
        self.volumes: list[str] = []
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{f' — {detail}' if detail else ''}", flush=True)
        if not ok:
            self.failures.append(name)
            raise Failure(name)

    def volume(self, name: str, *, owned: bool = False) -> str:
        full = f"ie-own-{self.tag}-{name}"
        docker("volume", "create", full)
        self.volumes.append(full)
        if owned:  # a volume first mounted outside /data: make it the engine user's
            setup = ["run", "--rm", "--user", "root", "-v", f"{full}:/v", self.image]
            docker(*setup, "chown", "engine:engine", "/v")
        return full

    def engine(self, name: str, port: int, mounts: list[str], env: dict[str, str]) -> str:
        full = f"ie-own-{self.tag}-{name}"
        args = ["run", "-d", "--name", full, "-p", f"127.0.0.1:{port}:8000"]
        base_env = {
            "IE_HOST": "0.0.0.0",
            "IE_ALLOW_NETWORK_BIND": "true",
            "IE_REQUIRE_AUTH": "true",
            "IE_DATA_DIR": "/data",
        }
        for key, value in {**base_env, **env}.items():
            args += ["-e", f"{key}={value}"]
        for mount in mounts:
            args += ["-v", mount]
        docker(*args, self.image)
        self.containers.append(full)
        return full

    def healthy(self, port: int, timeout: float = 120) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if http_json(port, "/healthz")[0] == 200:
                return True
            time.sleep(0.5)
        return False

    def exited(self, container: str, timeout: float = 120) -> tuple[bool, str, str]:
        """Waits (bounded) for the container to stop; returns (stopped, exit code, logs)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = docker("inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", container)
            running, code = state.stdout.split()
            if running == "false":
                logs = docker("logs", container, check=False)
                return True, code, logs.stdout + logs.stderr
            time.sleep(0.5)
        return False, "", ""

    def owner_key(self, container: str) -> str:
        created = docker("exec", container, "inference-engine", "keys", "create", "--label", "e2e")
        key = str(json.loads(created.stdout)["token"])
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::add-mask::{key}", flush=True)
        return key

    def cleanup(self) -> None:
        for container in self.containers:
            docker("rm", "-f", container, check=False)
        for volume in self.volumes:
            docker("volume", "rm", "-f", volume, check=False)

    # -- scenario 1 -----------------------------------------------------------

    def shared_model_volume(self) -> None:
        models = self.volume("models", owned=True)
        data_a, data_b = self.volume("data-a"), self.volume("data-b")
        owner = self.engine("owner", 18101, [f"{data_a}:/data", f"{models}:/data/models"], {})
        self.check("owner engine starts on its store", self.healthy(18101))

        # Different database volume, same model volume at a different mount path.
        second = self.engine(
            "second",
            18102,
            [f"{data_b}:/data", f"{models}:/srv/shared-models"],
            {"IE_MODELS_DIR": "/srv/shared-models"},
        )
        stopped, code, logs = self.exited(second)
        self.check(
            "second engine sharing the model volume refuses to start",
            stopped and code != "0" and LOCK_REFUSAL in logs,
            f"exit={code}",
        )
        self.check("owner still serving", http_json(18101, "/healthz")[0] == 200)

        docker("kill", owner)  # SIGKILL: the OS releases its locks
        docker("start", second)
        self.check("after the owner is killed, the second engine starts", self.healthy(18102))
        docker("start", owner)
        stopped, code, logs = self.exited(owner)
        self.check(
            "the restarted former owner now refuses (lock file reused, no stale lock)",
            stopped and code != "0" and LOCK_REFUSAL in logs,
            f"exit={code}",
        )
        docker("rm", "-f", owner, second, check=False)

    # -- scenario 2 -----------------------------------------------------------

    def interrupted(self, barrier: str, port: int) -> None:
        data = self.volume(f"data-{barrier}")
        child = f"ie-own-{self.tag}-child-{barrier}"
        marker = f"/data/{barrier}.reached"
        mounts = ["-v", f"{data}:/data", "-v", f"{CHILD_DIR}:/opt/test-support:ro"]
        script = ["python", "/opt/test-support/download_child.py", "/data", barrier, marker]
        docker("run", "-d", "--name", child, *mounts, self.image, *script)
        self.containers.append(child)
        deadline = time.monotonic() + 120
        reached = False
        while time.monotonic() < deadline and not reached:
            reached = docker("exec", child, "test", "-f", marker, check=False).returncode == 0
            if not reached:
                time.sleep(0.5)
        self.check(f"[{barrier}] child download reached its barrier in the image", reached)

        engine = self.engine(f"engine-{barrier}", port, [f"{data}:/data"], {})
        stopped, code, logs = self.exited(engine)
        self.check(
            f"[{barrier}] an engine on the child's volume refuses while the child lives",
            stopped and code != "0" and "owns this database" in logs,
            f"exit={code}",
        )
        docker("kill", child)
        docker("start", engine)
        self.check(f"[{barrier}] engine starts after the child is killed", self.healthy(port))
        key = self.owner_key(engine)
        status, body = http_json(port, "/admin/models", key)
        models = (body or {}).get("models", [])
        ok = (
            status == 200
            and len(models) == 1
            and models[0].get("status") == "error"
            and str(models[0].get("error", "")).startswith(INTERRUPTED_PREFIX)
            and models[0].get("sha256") is None
        )
        self.check(f"[{barrier}] interrupted download recovered as error", ok, f"status={status}")
        listing = docker("exec", engine, "ls", "-a", "/data/models").stdout.split()
        expected = {"m.gguf.part"} if barrier == "read" else {"m.gguf"}
        self.check(
            f"[{barrier}] files kept as found (plus the reserved lock file)",
            expected <= set(listing) and ".engine-model-store.lock" in listing,
            " ".join(sorted(set(listing) - {".", ".."})),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    run = Run(args.image)
    try:
        run.shared_model_volume()
        run.interrupted("read", 18111)
        run.interrupted("finalize", 18112)
    except Failure:
        pass
    except Exception as exc:  # surface unexpected errors as a failed run
        print(f"[FAIL] unexpected error — {type(exc).__name__}: {exc}", flush=True)
        run.failures.append("unexpected error")
    finally:
        if run.failures:
            for container in run.containers:
                print(f"--- {container} logs (tail) ---", flush=True)
                subprocess.run(["docker", "logs", "--tail", "40", container])
        run.cleanup()
    if run.failures:
        print(f"IMAGE OWNERSHIP E2E FAILED: {run.failures}")
        return 1
    print("IMAGE OWNERSHIP E2E PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
