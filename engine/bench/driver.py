"""Async HTTP load driver (Block 12.2b).

Fires a generated :class:`~engine.bench.workloads.Workload` at a running engine's
public OpenAI-compatible API over ``httpx``, honouring the concurrency schedule,
parsing OpenAI SSE correctly, and recording safe client-side
:class:`~engine.bench.metrics.RequestSample`s. Optionally captures a before/after
``/admin/routes`` aggregate delta under an exclusive-target run.

Kept thin: monotonic timing around each request, never blocking the event loop,
no response bodies or credentials in samples/reports. ``httpx`` is imported lazily
so the pure modules (and ``compare``) work without the ``bench`` extra.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from engine.bench.metrics import RequestSample
from engine.bench.workloads import RequestSpec, Workload

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx


def _import_httpx() -> Any:
    try:
        import httpx
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via install path
        raise SystemExit(
            "the benchmark driver needs httpx; install it with: pip install '.[bench]'"
        ) from exc
    return httpx


def _body(spec: RequestSpec) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": spec.model,
        "messages": [{"role": r, "content": c} for r, c in spec.messages],
        "max_tokens": spec.max_tokens,
        "stream": True,
    }
    if spec.response_format is not None:
        body["response_format"] = spec.response_format
    return body


def _content_delta(payload: dict[str, Any]) -> str | None:
    """The non-empty ``choices[0].delta.content`` of an OpenAI chunk, else None."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    return content if isinstance(content, str) and content else None


async def _one_request(
    client: httpx.AsyncClient, spec: RequestSpec, headers: dict[str, str]
) -> RequestSample:
    httpx = _import_httpx()
    start = time.monotonic()
    ttft_ms: float | None = None
    chunks = 0
    first = False
    outcome = "ok"
    failure: str | None = None
    shed = False
    try:
        async with client.stream(
            "POST", "/v1/chat/completions", json=_body(spec), headers=headers
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                total = (time.monotonic() - start) * 1000.0
                if resp.status_code == 429:
                    return RequestSample(
                        spec.slice, spec.model, None, total, 0, "error", False, None, True
                    )
                return RequestSample(
                    spec.slice, spec.model, None, total, 0, "error", False, "pre_stream", False
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:  # pragma: no cover - defensive
                    continue
                if isinstance(payload, dict) and "error" in payload:
                    failure = "post_stream" if first else "pre_stream"
                    outcome = "error"
                    break
                content = _content_delta(payload) if isinstance(payload, dict) else None
                if content is None:
                    continue  # role-only opener / finish frame
                if not first:
                    first = True
                    ttft_ms = (time.monotonic() - start) * 1000.0
                chunks += 1
                if spec.cancel_after_chunks is not None and chunks >= spec.cancel_after_chunks:
                    outcome = "cancelled"
                    break
    except (httpx.TransportError, httpx.HTTPError) as exc:  # noqa: F841 - body/secret never logged
        total = (time.monotonic() - start) * 1000.0
        failure = "post_stream" if first else "pre_stream"
        return RequestSample(
            spec.slice, spec.model, ttft_ms, total, chunks, "error", first, failure, False
        )
    total = (time.monotonic() - start) * 1000.0
    return RequestSample(
        spec.slice, spec.model, ttft_ms, total, chunks, outcome, first, failure, shed
    )


async def _run_phase(
    client: httpx.AsyncClient,
    specs: list[RequestSpec],
    concurrency: int,
    headers: dict[str, str],
    *,
    collect: bool = True,
) -> list[RequestSample]:
    if not specs:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(spec: RequestSpec) -> RequestSample:
        async with sem:
            return await _one_request(client, spec, headers)

    results = await asyncio.gather(*(_guarded(s) for s in specs))
    return list(results) if collect else []


async def _routes_snapshot(client: httpx.AsyncClient, headers: dict[str, str]) -> dict | None:
    try:
        resp = await client.get("/admin/routes", headers=headers)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:  # noqa: BLE001 - optional; unavailability must not fail the run
        return None


def _routes_delta(before: dict, after: dict, wall_s: float) -> dict[str, object]:
    def totals(snap: dict) -> dict[str, float]:
        t = snap.get("totals", {}) if isinstance(snap, dict) else {}
        rows = snap.get("routes", []) if isinstance(snap, dict) else []
        completion = sum(float(r.get("completion_tokens", 0) or 0) for r in rows)
        return {
            "requests": float(t.get("requests", 0) or 0),
            "cost": float(t.get("cost", 0) or 0),
            "completion_tokens": completion,
        }

    b, a = totals(before), totals(after)
    completion_delta = a["completion_tokens"] - b["completion_tokens"]
    return {
        "source": "admin_routes_delta",
        "exclusive_target": True,
        "requests": a["requests"] - b["requests"],
        "completion_tokens": completion_delta,
        "cost": a["cost"] - b["cost"],
        "output_tps": (completion_delta / wall_s) if wall_s > 0 else None,
    }


def _warmup_specs(workload: Workload) -> list[RequestSpec]:
    n = workload.warmup_requests
    return [
        RequestSpec("warmup", workload.model, (("user", f"warmup {i}"),), True, workload.max_tokens)
        for i in range(n)
    ]


async def run_workload(
    base_url: str,
    workload: Workload,
    *,
    gen_api_key: str | None = None,
    admin_api_key: str | None = None,
    exclusive_target: bool = False,
    request_timeout_s: float = 120.0,
    settle_s: float = 0.2,
) -> tuple[list[RequestSample], float, dict | None]:
    """Run the workload against ``base_url``; return (samples, wall_s, route_delta)."""
    httpx = _import_httpx()
    specs = workload.generate()
    main_specs = [s for s in specs if s.phase == "main"]
    burst_specs = [s for s in specs if s.phase == "saturation"]
    gen_headers = {"authorization": f"Bearer {gen_api_key}"} if gen_api_key else {}
    admin_headers = {"authorization": f"Bearer {admin_api_key}"} if admin_api_key else {}

    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), timeout=request_timeout_s
    ) as client:
        await _run_phase(
            client, _warmup_specs(workload), workload.concurrency, gen_headers, collect=False
        )
        want_routes = bool(admin_api_key) and exclusive_target
        before = await _routes_snapshot(client, admin_headers) if want_routes else None
        t0 = time.monotonic()
        samples = await _run_phase(client, main_specs, workload.concurrency, gen_headers)
        samples += await _run_phase(client, burst_specs, workload.burst_concurrency, gen_headers)
        wall_s = time.monotonic() - t0
        await asyncio.sleep(settle_s)  # let the server release cancelled work
        after = await _routes_snapshot(client, admin_headers) if want_routes else None

    route_delta = _routes_delta(before, after, wall_s) if before and after else None
    return samples, wall_s, route_delta
