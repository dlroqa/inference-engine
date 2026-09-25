"""Image-level end-to-end test for a release candidate (see docs/releasing.md).

Runs the *exact built image* through the owner deployment (deploy/compose.yaml)
on a fresh persistent volume and proves the artifact — not the source checkout —
works: health, not-ready-before-model, dashboard, auth over the network path,
owner key via the container CLI, checksum-verified GGUF import + load, streamed
OpenAI generation, operator API with the key, persistence across restart, and a
clean drain on stop. ``/version`` must report exactly the candidate's release
version, commit, and build date. Stdlib only; needs Docker + the Compose plugin.

The owner key is never printed (it is masked in GitHub Actions logs).

Usage:
    python scripts/release_image_e2e.py --image ghcr.io/dlroqa/inference-engine:candidate \
        --expect-image-id sha256:<config digest> --model ~/models/tiny.gguf \
        --model-sha256 <hex> --expect-version 0.1.0 --expect-commit <40-hex sha> \
        --expect-built-at 2026-09-24T00:00:00Z
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SERVICE = "inference-engine"
BASE = "http://127.0.0.1:8000"
MODEL_IN_CONTAINER = "/data/models/e2e-tiny.gguf"
MODEL_NAME = "e2e-tiny"
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# The release build date form (validate job: `date -u +%Y-%m-%dT%H:%M:%SZ`).
BUILT_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class E2EFailure(Exception):
    pass


def _matching(pattern: re.Pattern[str], what: str) -> Callable[[str], str]:
    def parse(value: str) -> str:
        if not pattern.match(value):
            raise argparse.ArgumentTypeError(f"not a {what}: {value!r}")
        return value

    return parse


release_version = _matching(VERSION_RE, "MAJOR.MINOR.PATCH release version")
commit_sha = _matching(COMMIT_RE, "40-character lowercase commit SHA")
_built_at_form = _matching(BUILT_AT_RE, "UTC RFC 3339 build timestamp (YYYY-MM-DDTHH:MM:SSZ)")


def built_at(value: str) -> str:
    """An RFC 3339 UTC timestamp (``YYYY-MM-DDTHH:MM:SSZ``) that is a real instant."""
    _built_at_form(value)
    try:
        dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid build timestamp {value!r}: {exc}") from exc
    return value


def version_mismatches(body: Any, expected: dict[str, str]) -> list[str]:
    """Fields of a ``/version`` JSON body that differ from the candidate metadata.

    Every expected field must be present and exactly equal; extra fields are fine.
    """
    if not isinstance(body, dict):
        return [f"/version body is not a JSON object: {body!r}"]
    return [
        f"{field}={body.get(field)!r} (expected {want!r})"
        for field, want in expected.items()
        if body.get(field) != want
    ]


def http(
    method: str,
    path: str,
    *,
    key: str | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return 0, b""


def http_json(method: str, path: str, **kw: Any) -> tuple[int, Any]:
    status, raw = http(method, path, **kw)
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, None


def wait_status(path: str, want: int, timeout: float, key: str | None = None) -> int:
    deadline = time.monotonic() + timeout
    status = 0
    while time.monotonic() < deadline:
        status, _ = http("GET", path, key=key, timeout=5)
        if status == want:
            return status
        time.sleep(1)
    return status


class Compose:
    def __init__(self, compose_file: Path, project: str, image: str) -> None:
        self.base = ["docker", "compose", "-p", project, "-f", str(compose_file)]
        self.env = {**os.environ, "IMAGE": image}

    def run(
        self, *args: str, input_bytes: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        proc = subprocess.run(
            [*self.base, *args], env=self.env, input=input_bytes, capture_output=True
        )
        if check and proc.returncode != 0:
            raise E2EFailure(
                f"docker compose {' '.join(args[:2])} failed ({proc.returncode}): "
                f"{proc.stderr.decode(errors='replace')[-2000:]}"
            )
        return proc

    def container_id(self) -> str:
        return self.run("ps", "-a", "-q", SERVICE).stdout.decode().strip()


def docker_inspect(container: str, fmt: str) -> str:
    return subprocess.run(
        ["docker", "inspect", "-f", fmt, container], capture_output=True, text=True, check=True
    ).stdout.strip()


def read_stream(key: str) -> tuple[list[str], str | None, bool]:
    """POST a streamed chat completion; return content pieces, finish reason, and
    whether ``[DONE]`` terminated the stream after the finishing chunk."""
    body = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": "Count from one to five."}],
        "max_tokens": 24,
        "stream": True,
    }
    req = urllib.request.Request(
        f"{BASE}/v1/chat/completions", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    pieces: list[str] = []
    finish: str | None = None
    done_after_finish = False
    ids: set[str] = set()
    with urllib.request.urlopen(req, timeout=120) as resp:
        if resp.status != 200:
            raise E2EFailure(f"stream status {resp.status}")
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                done_after_finish = finish is not None
                break
            if finish is not None:
                raise E2EFailure("chunk received after the finishing chunk")
            chunk = json.loads(payload)
            ids.add(chunk.get("id", ""))
            choice = chunk["choices"][0]
            if choice.get("index", 0) != 0:
                raise E2EFailure(f"unexpected choice index {choice.get('index')}")
            if choice.get("delta", {}).get("content"):
                pieces.append(choice["delta"]["content"])
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    if len(ids) != 1:
        raise E2EFailure(f"stream chunks carried {len(ids)} different ids")
    return pieces, finish, done_after_finish


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="Local image reference to test.")
    parser.add_argument(
        "--expect-image-id",
        required=True,
        help="Config digest (sha256:...) the running container's image must have.",
    )
    parser.add_argument("--model", type=Path, required=True, help="Checksum-verified GGUF.")
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument(
        "--expect-version", type=release_version, required=True, help="Release version."
    )
    parser.add_argument(
        "--expect-commit", type=commit_sha, required=True, help="Tag commit SHA (40 hex)."
    )
    parser.add_argument(
        "--expect-built-at", type=built_at, required=True, help="UTC RFC 3339 build date."
    )
    parser.add_argument("--compose", type=Path, default=ROOT / "deploy" / "compose.yaml")
    parser.add_argument("--project", default="ie-release-e2e")
    parser.add_argument("--grace-seconds", type=float, default=40.0)
    parser.add_argument("--keep", action="store_true", help="Leave the stack + volume up.")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    expected_version = {
        "version": args.expect_version,
        "commit": args.expect_commit,
        "built_at": args.expect_built_at,
    }

    compose = Compose(args.compose, args.project, args.image)
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{f' — {detail}' if detail else ''}", flush=True)
        if not ok:
            failures.append(name)
            raise E2EFailure(name)

    try:
        # Fresh project => fresh named volume.
        compose.run("down", "-v", "--remove-orphans", check=False)
        compose.run("up", "-d", "--pull", "never", "--no-build")
        cid = compose.container_id()
        image_id = docker_inspect(cid, "{{.Image}}")
        check(
            "container runs the exact candidate image", image_id == args.expect_image_id, image_id
        )
        volumes = docker_inspect(cid, "{{range .Mounts}}{{.Type}}:{{.Destination}} {{end}}")
        check("persistent named volume at /data", "volume:/data" in volumes, volumes)

        check("/healthz 200", wait_status("/healthz", 200, 120) == 200)
        status, body = http_json("GET", "/version")
        wrong = version_mismatches(body, expected_version) if status == 200 else []
        check(
            "/version reports the candidate version, commit, and build date",
            status == 200 and not wrong,
            f"status={status} {'; '.join(wrong)}".strip(),
        )
        status, _ = http("GET", "/readyz")
        check("/readyz not ready before a model is loaded", status == 503, f"status={status}")
        status, raw = http("GET", "/dashboard/")
        check("/dashboard served", status == 200 and b"<html" in raw.lower(), f"status={status}")

        for method, path in (
            ("GET", "/admin/overview"),
            ("GET", "/admin/keys"),
            ("GET", "/admin/models"),
            ("GET", "/metrics"),
            ("GET", "/logs"),
            ("POST", "/admin/model/unload"),
        ):
            status, _ = http(method, path)
            check(f"unauthenticated {method} {path} -> 401", status == 401, f"status={status}")
        created = compose.run(
            "exec", "-T", SERVICE, "inference-engine", "keys", "create", "--label", "owner"
        )
        key = json.loads(created.stdout.decode())["token"]
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::add-mask::{key}", flush=True)
        check("owner key created via the container CLI", bool(key))

        compose.run(
            "exec",
            "-T",
            SERVICE,
            "sh",
            "-c",
            f"mkdir -p /data/models && cat > {MODEL_IN_CONTAINER}",
            input_bytes=args.model.read_bytes(),
        )
        status, model = http_json(
            "POST",
            "/admin/models/import",
            key=key,
            body={"path": MODEL_IN_CONTAINER, "name": MODEL_NAME},
            timeout=300,
        )
        check(
            "GGUF imported with verified checksum",
            status == 200
            and model.get("status") == "ready"
            and model.get("sha256") == args.model_sha256,
            f"status={status} sha256={(model or {}).get('sha256')}",
        )
        model_id = model["id"]

        status, _ = http_json("POST", f"/admin/models/{model_id}/load", key=key, timeout=300)
        check("model load", status == 200, f"status={status}")
        check("/readyz 200 once the model is loaded", wait_status("/readyz", 200, 120) == 200)
        status, _ = http_json(
            "POST",
            "/v1/chat/completions",
            body={"model": MODEL_NAME, "messages": [{"role": "user", "content": "hi"}]},
        )
        check("unauthenticated generation -> 401", status == 401, f"status={status}")

        pieces, finish, done = read_stream(key)
        check(
            "streamed OpenAI generation (ordered content + terminal completion)",
            bool("".join(pieces).strip()) and finish in {"stop", "length"} and done,
            f"{len(pieces)} chunks, finish={finish}, text={''.join(pieces)!r}",
        )

        status, _ = http_json("GET", "/admin/overview", key=key)
        check("operator API with the owner key", status == 200, f"status={status}")
        status, _ = http_json("GET", "/metrics", key=key)
        check("operator metrics with the owner key", status == 200, f"status={status}")

        compose.run("restart", SERVICE)
        check("/healthz 200 after restart", wait_status("/healthz", 200, 120) == 200)
        status, models = http_json("GET", "/admin/models", key=key)
        listed = (models or {}).get("models", [])
        check(
            "database + key + model registry persist across restart",
            status == 200 and any(m.get("id") == model_id for m in listed),
            f"status={status} models={len(listed)}",
        )
        status, _ = http_json("POST", f"/admin/models/{model_id}/load", key=key, timeout=300)
        check("persisted model reloads after restart", status == 200, f"status={status}")
        status, gen = http_json(
            "POST",
            "/v1/chat/completions",
            key=key,
            body={
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": "Say hi"}],
                "max_tokens": 8,
            },
            timeout=120,
        )
        text = ((gen or {}).get("choices") or [{}])[0].get("message", {}).get("content")
        check("generation after restart", status == 200 and bool(text), f"text={text!r}")

        started = time.monotonic()
        compose.run("stop", SERVICE)
        elapsed = time.monotonic() - started
        exit_code = docker_inspect(cid, "{{.State.ExitCode}}")
        logs = subprocess.run(["docker", "logs", cid], capture_output=True, text=True).stdout
        logs += subprocess.run(["docker", "logs", cid], capture_output=True, text=True).stderr
        check(
            "clean drain on stop within the grace period",
            elapsed < args.grace_seconds and '"event": "drain_complete"' in logs,
            f"{elapsed:.1f}s < {args.grace_seconds:.0f}s, exit={exit_code}",
        )
        check("not killed at the grace deadline", exit_code != "137", f"exit={exit_code}")
    except E2EFailure:
        pass
    except Exception as exc:  # surface unexpected errors as a failed run
        print(f"[FAIL] unexpected error — {type(exc).__name__}: {exc}", flush=True)
        failures.append("unexpected error")
    finally:
        if failures:
            cid = compose.container_id()
            if cid:
                print("--- container logs (tail) ---")
                subprocess.run(["docker", "logs", "--tail", "80", cid])
        if not args.keep:
            compose.run("down", "-v", "--remove-orphans", check=False)

    if failures:
        print(f"IMAGE E2E FAILED: {failures}")
        return 1
    print("IMAGE E2E PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
