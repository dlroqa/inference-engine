"""CLI for the benchmark harness (Block 12.2b): ``python -m engine.bench``.

Two subcommands:

* ``run`` drives a workload against a live engine and writes a ``RunReport`` JSON
  (needs the ``bench`` extra for httpx, and a live endpoint / operator GPU);
* ``compare`` applies the pure §8.1/§8.2 gate to two reports and exits non-zero
  when the candidate does not clear it (no live server, CI-testable).

Runtime identity (build info, Python/platform, timestamp, sanitized origin) is
collected *here*, at the boundary, and injected into the pure report so
``report.py`` stays free of clock/env/platform/network/Git access.
"""

from __future__ import annotations

import argparse
import platform
import sys
from datetime import UTC, datetime
from typing import Any

from engine.bench import HARNESS_VERSION
from engine.bench.gate import RegressionBudget, compare
from engine.bench.metrics import aggregate
from engine.bench.report import IdentityStamp, ReportError, RunReport, sanitize_origin
from engine.bench.workloads import Workload, smoke_workload, standard_workload
from engine.buildinfo import build_info


def _identity(base_url: str) -> IdentityStamp:
    return IdentityStamp(
        harness_version=HARNESS_VERSION,
        build_info=build_info(),
        python_version=platform.python_version(),
        platform=f"{platform.platform()} ({platform.processor() or 'unknown-cpu'})",
        timestamp=datetime.now(UTC).isoformat(),
        origin=sanitize_origin(base_url),
    )


def _build_workload(args: argparse.Namespace) -> Workload:
    if args.workload == "smoke":
        return smoke_workload(model=args.model, seed=args.seed)
    return standard_workload(
        model=args.model,
        total_requests=args.total_requests,
        seed=args.seed,
        concurrency=args.concurrency,
        burst_concurrency=args.burst_concurrency,
        warmup_requests=args.warmup,
        max_tokens=args.max_tokens,
        prefix_chars=args.prefix_chars,
        long_context_chars=args.long_context_chars,
        structured_supported=args.structured,
    )


def _cmd_run(args: argparse.Namespace) -> int:
    import asyncio

    from engine.bench.driver import run_workload

    workload = _build_workload(args)
    samples, wall_s, route_delta = asyncio.run(
        run_workload(
            args.base_url,
            workload,
            gen_api_key=args.api_key,
            admin_api_key=args.admin_api_key,
            exclusive_target=args.exclusive_target,
            request_timeout_s=args.request_timeout,
        )
    )
    results = aggregate(samples, wall_s=wall_s)
    metric_sources = {
        "ttft": "client",
        "latency": "client",
        "accepted_rate": "client",
        "shed_rate": "client",
        "error_rate": "client",
        "output_tps": "admin_routes_delta" if route_delta else "unavailable",
        "cost": "admin_routes_delta" if route_delta else "unavailable",
    }
    report = RunReport(
        identity=_identity(args.base_url),
        manifest=workload.manifest(),
        results=results,
        route_delta=route_delta,
        metric_sources=metric_sources,
    )
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(report.to_json())
    overall: Any = results["overall"]
    print(
        f"wrote {args.out}: {overall['attempted']} attempted, {overall['admitted']} admitted, "
        f"accepted_rate={overall['accepted_rate']}, route_metrics="
        f"{'available' if route_delta else 'unavailable'}"
    )
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    try:
        with open(args.baseline, encoding="utf-8") as fh:
            baseline = RunReport.from_json(fh.read())
        with open(args.candidate, encoding="utf-8") as fh:
            candidate = RunReport.from_json(fh.read())
    except ReportError as exc:
        print(f"compare: {exc}", file=sys.stderr)
        return 2
    budget = RegressionBudget(
        error_rate_pp=args.error_budget_pp,
        latency_pct=args.latency_budget_pct,
        shed_rate_pp=args.shed_budget_pp,
    )
    outcome = compare(
        baseline,
        candidate,
        target_metric=args.target_metric,
        target_slice=args.target_slice,
        min_improvement=args.min_improvement,
        budget=budget,
    )
    print("GATE: PASS" if outcome.passed else "GATE: FAIL")
    for reason in outcome.reasons:
        print(f"  - {reason}")
    return 0 if outcome.passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m engine.bench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="drive a workload against a live engine")
    run.add_argument("--base-url", required=True)
    run.add_argument("--workload", choices=["smoke", "standard"], default="smoke")
    run.add_argument("--model", required=True, help="the OpenAI 'model' each request requests")
    run.add_argument("--out", required=True, help="path to write the RunReport JSON")
    run.add_argument("--seed", type=int, default=1234)
    run.add_argument("--total-requests", type=int, default=200)
    run.add_argument("--concurrency", type=int, default=8)
    run.add_argument("--burst-concurrency", type=int, default=64)
    run.add_argument("--warmup", type=int, default=8)
    run.add_argument("--max-tokens", type=int, default=32)
    run.add_argument("--prefix-chars", type=int, default=256)
    run.add_argument("--long-context-chars", type=int, default=2000)
    run.add_argument(
        "--structured", action="store_true", help="model/route supports structured output"
    )
    run.add_argument("--api-key", default=None, help="generation credential (never stored/logged)")
    run.add_argument("--admin-api-key", default=None, help="operator credential for /admin/routes")
    run.add_argument(
        "--exclusive-target", action="store_true", help="no other traffic during the run"
    )
    run.add_argument("--request-timeout", type=float, default=120.0)
    run.set_defaults(func=_cmd_run)

    cmp_ = sub.add_parser("compare", help="apply the §8.1/§8.2 gate to two reports")
    cmp_.add_argument("--baseline", required=True)
    cmp_.add_argument("--candidate", required=True)
    cmp_.add_argument("--target-metric", required=True)
    cmp_.add_argument("--target-slice", default="overall")
    cmp_.add_argument("--min-improvement", type=float, default=0.15)
    cmp_.add_argument("--error-budget-pp", type=float, default=0.5)
    cmp_.add_argument("--latency-budget-pct", type=float, default=0.05)
    cmp_.add_argument("--shed-budget-pp", type=float, default=2.0)
    cmp_.set_defaults(func=_cmd_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    raise SystemExit(main())
