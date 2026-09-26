#!/usr/bin/env python3
"""Export the engine's HTTP + WebSocket route inventory for the dashboard.

Writes ``dashboard/src/lib/routes.generated.json``: one entry per documented route
with its method, path, auth gate, and handler. The dashboard's wiring registry
(``dashboard/src/lib/wiring.ts``) is checked against this file by vitest, and CI
regenerates it and fails on a diff, so the UI's "wired to" explanations can never
silently drift from the backend.

Usage::

    python scripts/export_routes.py            # write the file
    python scripts/export_routes.py --check    # exit 1 if the file is stale
"""

from __future__ import annotations

import argparse
import difflib
import inspect
import json
import sys
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "dashboard" / "src" / "lib" / "routes.generated.json"


def _gate(path: str, module: str, source: str) -> str:
    """Classify a handler's auth gate (deterministic, reviewed by hand).

    ``/v1/models`` never calls the gateway, so it is public; the other inference
    edges authorize through the shared serving pipeline with an API key.
    """
    if path.startswith("/ws/") or "require_operator(" in source:
        return "operator"
    if path.startswith("/client/"):
        return "client"
    if path == "/billing/webhooks/stripe":
        return "stripe_signature"
    if path == "/v1/models":
        return "public"
    if module.endswith(("openai_router", "anthropic_router")):
        return "api_key"
    return "public"


_CHILD_ATTRS = ("routes", "router", "original_router", "_router", "app")


def _children(route: Any) -> list[Any] | None:
    """Sub-routes of a container (a mount or an included router), duck-typed."""
    for attr in _CHILD_ATTRS:
        value = getattr(route, attr, None)
        if value is None or value is route:
            continue
        if isinstance(value, list | tuple):
            return list(value)
        nested = getattr(value, "routes", None)
        if isinstance(nested, list | tuple):
            return list(nested)
    return None


def _walk(routes: Iterable[Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield ``(full_path, route)`` for every leaf route, descending into mounts
    and included routers without depending on FastAPI's private classes."""
    for route in routes:
        if getattr(route, "endpoint", None) is not None:
            yield prefix + getattr(route, "path", ""), route
            continue
        children = _children(route)
        if children:
            own = getattr(route, "path", None) or getattr(route, "prefix", None) or ""
            yield from _walk(children, prefix + own)


def build_inventory() -> list[dict[str, Any]]:
    from engine.config import Settings
    from engine.main import create_app

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Settings(data_dir=Path(tmp) / "data"))
        # OpenAPI is FastAPI's own resolution of full HTTP paths; the walk must
        # agree with it exactly, so a routing-internals change fails loudly.
        spec_paths = app.openapi()["paths"]
    documented = {
        (method.upper(), path)
        for path, ops in spec_paths.items()
        for method in ops
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }
    rows: list[dict[str, Any]] = []
    for path, route in _walk(app.router.routes):
        endpoint = route.endpoint
        module = getattr(endpoint, "__module__", "")
        if not module.startswith("engine.") or not getattr(route, "include_in_schema", True):
            continue  # FastAPI's own docs routes, the dashboard redirect
        methods = getattr(route, "methods", None)
        if methods:
            source = inspect.getsource(endpoint)
            for method in sorted(set(methods) - {"HEAD", "OPTIONS"}):
                rows.append(
                    {
                        "method": method,
                        "path": path,
                        "gate": _gate(path, module, source),
                        "module": module,
                        "handler": endpoint.__name__,
                    }
                )
        elif "websocket" in type(route).__name__.lower():
            rows.append(
                {
                    "method": "WS",
                    "path": path,
                    "gate": "operator",
                    "module": module,
                    "handler": endpoint.__name__,
                }
            )
    rows.sort(key=lambda r: (r["path"], r["method"]))
    walked = {(r["method"], r["path"]) for r in rows if r["method"] != "WS"}
    if not rows or walked != documented:
        kinds = sorted({type(r).__name__ for r in app.router.routes})
        sample = next((r for r in app.router.routes if getattr(r, "endpoint", None) is None), None)
        attrs = sorted(a for a in dir(sample) if not a.startswith("__")) if sample else []
        raise SystemExit(
            "route walk disagrees with OpenAPI\n"
            f"  missing from walk: {sorted(documented - walked)}\n"
            f"  extra in walk:     {sorted(walked - documented)}\n"
            f"  top-level types:   {kinds}\n"
            f"  container attrs:   {attrs}"
        )
    return rows


def render() -> str:
    payload = {
        "_comment": "Generated by scripts/export_routes.py — do not edit by hand.",
        "routes": build_inventory(),
    }
    return json.dumps(payload, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(
                f"{OUT.relative_to(ROOT)} is stale; run: python scripts/export_routes.py",
                file=sys.stderr,
            )
            sys.stderr.writelines(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    "committed",
                    "generated",
                )
            )
            return 1
        print("route inventory is current")
        return 0
    OUT.write_text(text, encoding="utf-8")
    count = len(json.loads(text)["routes"])
    print(f"wrote {OUT.relative_to(ROOT)} ({count} routes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
