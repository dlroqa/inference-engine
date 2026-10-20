"""``scripts/export_routes.py`` output stability (A5).

The dashboard's wiring registry is checked against the committed route
inventory, so the inventory must be deterministic, complete and current. CI
also runs ``export_routes.py --check``; this keeps the same guarantees in the
test suite, with the reasons spelled out.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "export_routes", ROOT / "scripts" / "export_routes.py"
)
assert _spec and _spec.loader
export_routes = importlib.util.module_from_spec(_spec)
sys.modules["export_routes"] = export_routes
_spec.loader.exec_module(export_routes)


def test_render_is_deterministic() -> None:
    assert export_routes.render() == export_routes.render()


def test_rows_are_sorted_unique_and_complete() -> None:
    routes = json.loads(export_routes.render())["routes"]
    keys = [(r["path"], r["method"]) for r in routes]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))
    for r in routes:
        assert {"method", "path", "gate", "module", "handler"} <= set(r), r
        assert r["method"] not in ("HEAD", "OPTIONS"), r
    assert ("/ws/metrics", "WS") in keys
    assert ("/ws/feed", "WS") in keys


def test_the_committed_inventory_is_current() -> None:
    # The same comparison as `export_routes.py --check`, which prints the diff.
    assert export_routes.main(["--check"]) == 0
