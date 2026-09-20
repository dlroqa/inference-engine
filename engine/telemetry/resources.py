"""Process and system resource sampling.

Uses ``psutil`` for CPU, memory, disk, and process RSS. All access is defensive:
if ``psutil`` is missing or a metric is unavailable on the platform, the field is
reported as ``None`` (with a ``"available": false`` note) rather than raising, so
the metrics stream never fails because a probe is unsupported.

GPU metrics are **not** produced here: no tested GPU probe ships in this block, so
the GPU panel reports an explicit unavailable state (see :func:`gpu`). This keeps
the honesty rule — never present an untested/absent probe as real data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:  # psutil ships prebuilt wheels for all supported platforms.
    import psutil

    _PSUTIL_ERR: str | None = None
except Exception as exc:  # pragma: no cover - exercised only when psutil absent
    psutil = None
    _PSUTIL_ERR = f"{exc.__class__.__name__}: {exc}"


@dataclass(slots=True)
class MemoryStats:
    total: int | None = None
    used: int | None = None
    available: int | None = None
    percent: float | None = None


@dataclass(slots=True)
class DiskStats:
    path: str = ""
    total: int | None = None
    used: int | None = None
    free: int | None = None
    percent: float | None = None


class ResourceSampler:
    """Samples CPU/RAM/disk/RSS. Holds the psutil handles between calls.

    ``psutil.cpu_percent(interval=None)`` reports usage since the previous call,
    so the sampler primes it once at construction and reads deltas thereafter.
    """

    def __init__(self, *, disk_path: Path | None = None) -> None:
        self.available = psutil is not None
        self._disk_path = Path(disk_path) if disk_path is not None else Path.cwd()
        self._proc = psutil.Process() if self.available else None
        if self.available:
            # Prime the interval-less cpu_percent so the first real sample is a
            # delta rather than 0.0 / a blocking call.
            psutil.cpu_percent(interval=None)
            if self._proc is not None:
                self._proc.cpu_percent(interval=None)

    def cpu_percent(self) -> float | None:
        if not self.available:
            return None
        try:
            return float(psutil.cpu_percent(interval=None))
        except Exception:  # pragma: no cover - platform dependent
            return None

    def memory(self) -> MemoryStats:
        if not self.available:
            return MemoryStats()
        try:
            vm = psutil.virtual_memory()
            return MemoryStats(
                total=int(vm.total),
                used=int(vm.total - vm.available),
                available=int(vm.available),
                percent=float(vm.percent),
            )
        except Exception:  # pragma: no cover - platform dependent
            return MemoryStats()

    def process_rss(self) -> int | None:
        if not self.available or self._proc is None:
            return None
        try:
            return int(self._proc.memory_info().rss)
        except Exception:  # pragma: no cover - platform dependent
            return None

    def disk(self) -> DiskStats:
        target = self._disk_path
        # Walk up to the nearest existing ancestor so a not-yet-created data dir
        # still yields the containing filesystem's usage.
        while not target.exists() and target != target.parent:
            target = target.parent
        if not self.available:
            return DiskStats(path=str(self._disk_path))
        try:
            du = psutil.disk_usage(str(target))
            return DiskStats(
                path=str(self._disk_path),
                total=int(du.total),
                used=int(du.used),
                free=int(du.free),
                percent=float(du.percent),
            )
        except Exception:  # pragma: no cover - platform dependent
            return DiskStats(path=str(self._disk_path))

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": None if self.available else (_PSUTIL_ERR or "psutil unavailable"),
            "cpu_percent": self.cpu_percent(),
            "memory": asdict(self.memory()),
            "process_rss": self.process_rss(),
            "disk": asdict(self.disk()),
        }


def gpu() -> dict[str, Any]:
    """GPU panel state. No tested GPU probe ships in Block 4."""
    return {
        "available": False,
        "reason": "no tested GPU probe in this build",
    }
