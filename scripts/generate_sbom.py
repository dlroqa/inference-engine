"""Generate a minimal CycloneDX SBOM of the installed environment (Block 9).

Stdlib only (uses importlib.metadata), so it ships without extra dependencies.
Produces a dependency inventory an operator can archive with a release for
supply-chain visibility. Not a substitute for a full scanner, but an honest,
reproducible component list.

Usage:
    python scripts/generate_sbom.py --out sbom.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from importlib import metadata

from engine import __version__


def _components() -> list[dict[str, object]]:
    comps: list[dict[str, object]] = []
    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        name = dist.metadata["Name"]
        if not name:
            continue
        version = dist.version
        comps.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{name.lower()}@{version}",
            }
        )
    return comps


def build_sbom() -> dict[str, object]:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "metadata": {
            "timestamp": now,
            "component": {
                "type": "application",
                "name": "inference-engine",
                "version": __version__,
            },
        },
        "components": _components(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="-", help="Output path, or '-' for stdout.")
    args = parser.parse_args()
    doc = json.dumps(build_sbom(), indent=2)
    if args.out == "-":
        print(doc)
    else:
        with open(args.out, "w") as fh:
            fh.write(doc)
        print(f"wrote SBOM with {len(build_sbom()['components'])} components to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
