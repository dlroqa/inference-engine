"""Repeatable concurrency load test for a running Inference Engine (Block 7).

Fires a fixed number of chat-completion requests across a fixed number of
concurrent clients and prints an admission + latency summary: status breakdown
(including `429 engine_saturated` shedding), throughput, latency percentiles, and
the engine's own scheduler counters read from `GET /metrics`.

Stdlib only, so it ships with the package and needs no extra dependencies.

Document the hardware when you record a run (CPU model, core count, RAM, and
whether a GPU/AVX2 is present) so results are comparable. The reference profile is
run in CI on GitHub's AVX2 runners against the pinned SmolLM2-135M model; the local
sandbox CPU lacks AVX and cannot run a real GGUF model (see README).

Usage:
    python scripts/load_test.py --base-url http://127.0.0.1:8000 \
        --concurrency 16 --requests 128 --max-tokens 32 --model local-model
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


@dataclass
class Outcome:
    status: int
    latency_s: float
    code: str | None = None


def _post_chat(base: str, body: dict, api_key: str | None, timeout: float) -> Outcome:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{base}/v1/chat/completions", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return Outcome(status=resp.status, latency_s=time.monotonic() - start)
    except urllib.error.HTTPError as exc:
        code = None
        try:
            code = json.load(exc).get("error", {}).get("code")
        except Exception:
            pass
        return Outcome(status=exc.code, latency_s=time.monotonic() - start, code=code)
    except Exception:
        return Outcome(status=0, latency_s=time.monotonic() - start, code="transport_error")


def _get_json(base: str, path: str, api_key: str | None) -> dict:
    req = urllib.request.Request(f"{base}{path}", method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except Exception:
        return {}


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q / 100.0 * (len(ordered) - 1))))
    return ordered[idx]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="local-model")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--requests", type=int, default=128)
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument("--prompt", default="Write one sentence about the sea.")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    body = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
    }

    print(
        f"Load test → {base}  (concurrency={args.concurrency}, "
        f"requests={args.requests}, max_tokens={args.max_tokens})"
    )
    before = _get_json(base, "/metrics", args.api_key).get("scheduler")

    wall_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        outcomes = list(
            pool.map(
                lambda _: _post_chat(base, body, args.api_key, args.timeout),
                range(args.requests),
            )
        )
    wall = time.monotonic() - wall_start

    after = _get_json(base, "/metrics", args.api_key).get("scheduler")

    ok = [o for o in outcomes if o.status == 200]
    shed = [o for o in outcomes if o.status == 429]
    other = [o for o in outcomes if o.status not in (200, 429)]
    ok_latencies = [o.latency_s for o in ok]

    print("\n=== Results ===")
    print(f"wall time:        {wall:.2f}s  ({args.requests / wall:.1f} req/s attempted)")
    print(f"succeeded (200):  {len(ok)}")
    print(f"shed (429):       {len(shed)}")
    if other:
        codes = {}
        for o in other:
            key = f"{o.status}:{o.code}"
            codes[key] = codes.get(key, 0) + 1
        print(f"other:            {len(other)}  {codes}")
    if ok_latencies:
        print(
            "latency (200):    "
            f"p50={_pct(ok_latencies, 50) * 1000:.0f}ms  "
            f"p95={_pct(ok_latencies, 95) * 1000:.0f}ms  "
            f"p99={_pct(ok_latencies, 99) * 1000:.0f}ms  "
            f"max={max(ok_latencies) * 1000:.0f}ms  "
            f"mean={statistics.mean(ok_latencies) * 1000:.0f}ms"
        )
    if after:
        print("\n=== Engine scheduler (from /metrics) ===")
        for key in (
            "max_concurrency",
            "max_queue_depth",
            "admitted_total",
            "rejected_total",
            "rejected_queue_full",
            "rejected_timeout",
            "cancelled_total",
            "slow_consumer_total",
            "peak_queue_depth",
            "wait_ms_avg",
            "wait_ms_max",
        ):
            print(f"  {key:22} {after.get(key)}")
    elif before is None:
        print("\n(scheduler metrics unavailable — is this endpoint operator-gated?)")

    # Success criterion: no unexpected failures. Shedding (429) and success (200)
    # are both acceptable outcomes; transport errors / 5xx are not.
    if other:
        print("\nLOAD TEST FAILED: unexpected non-200/429 responses")
        return 1
    print("\nLOAD TEST OK: engine bounded load to success + retriable shedding")
    return 0


if __name__ == "__main__":
    sys.exit(main())
