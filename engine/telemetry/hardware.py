"""A static, secret-free hardware/platform summary.

Used by the diagnostics export and the (later) Settings/About view. Everything
here is non-sensitive host description: OS, processor, core counts, total RAM, and
notable CPU SIMD flags (which matter for llama.cpp performance and the AVX
limitation documented in the README). It is cheap to compute and cached.
"""

from __future__ import annotations

import functools
import platform
from typing import Any

from engine import __version__

try:
    import psutil

    _HAVE_PSUTIL = True
except Exception:  # pragma: no cover - exercised only when psutil absent
    psutil = None
    _HAVE_PSUTIL = False

# A small allow-list of interesting x86 SIMD flags for the summary.
_SIMD_FLAGS = ("avx512f", "avx2", "avx", "fma", "f16c", "sse4_2", "neon")


def _cpu_flags() -> list[str]:
    """Best-effort SIMD flag detection (Linux ``/proc/cpuinfo``)."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.lower().startswith("flags") or line.lower().startswith("features"):
                    present = set(line.split(":", 1)[1].split())
                    return [flag for flag in _SIMD_FLAGS if flag in present]
    except OSError:
        pass
    return []


@functools.lru_cache(maxsize=1)
def summary() -> dict[str, Any]:
    """Return the cached hardware summary dict (no secrets)."""
    mem_total: int | None = None
    physical: int | None = None
    if _HAVE_PSUTIL and psutil is not None:
        try:
            mem_total = int(psutil.virtual_memory().total)
        except Exception:  # pragma: no cover - platform dependent
            mem_total = None
        try:
            physical = psutil.cpu_count(logical=False)
        except Exception:  # pragma: no cover - platform dependent
            physical = None

    import os

    return {
        "engine_version": __version__,
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine(),
        "python_version": platform.python_version(),
        "cpu_count_logical": os.cpu_count(),
        "cpu_count_physical": physical,
        "cpu_simd_flags": _cpu_flags(),
        "memory_total": mem_total,
    }
