"""Release/build metadata for supply-chain visibility (Block 9).

The package version is authoritative; commit SHA and build date are injected at
image/build time via ``IE_BUILD_SHA`` / ``IE_BUILD_DATE`` and are ``None`` in a
plain source checkout. Surfaced on ``/healthz`` and ``/version`` so an operator
can tell exactly which build is running.
"""

from __future__ import annotations

import os
from typing import Any

from engine import __version__


def build_info() -> dict[str, Any]:
    return {
        "version": __version__,
        "commit": os.environ.get("IE_BUILD_SHA") or None,
        "built_at": os.environ.get("IE_BUILD_DATE") or None,
    }
