"""Host-compatibility assessment for a registry model.

Answers, honestly, *"can this host actually run this model?"* — the Block 6
requirement that a downloaded model is never implied runnable just because it is
listed. The states:

- ``ok``            — a usable backend exists and the model should fit.
- ``needs_backend`` — no runtime for it here: ``llama-cpp-python`` is not
  installed, or the CPU lacks AVX2 (the prebuilt llama.cpp requires it — the exact
  limitation of this sandbox). The model is a valid file; it just can't run here.
- ``too_large``     — a backend exists but the model likely won't fit in RAM.
- ``unknown``       — not enough information (e.g. size not yet known, or the
  platform doesn't expose CPU flags).

Checks avoid *importing* ``llama_cpp`` (importing/loading can hard-crash on a
non-AVX CPU); availability is probed with ``importlib.util.find_spec``.
"""

from __future__ import annotations

import enum
import importlib.util
from dataclasses import dataclass

from engine.telemetry import hardware


class Compat(enum.StrEnum):
    OK = "ok"
    NEEDS_BACKEND = "needs_backend"
    TOO_LARGE = "too_large"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class CompatReport:
    status: Compat
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status.value, "reason": self.reason}


def llama_backend_available() -> bool:
    """Whether ``llama-cpp-python`` is importable, without importing it."""
    return importlib.util.find_spec("llama_cpp") is not None


def assess(*, size_bytes: int | None, valid_gguf: bool = True) -> CompatReport:
    if not valid_gguf:
        return CompatReport(Compat.UNKNOWN, "not a recognized GGUF file")

    if not llama_backend_available():
        return CompatReport(
            Compat.NEEDS_BACKEND,
            "llama-cpp-python is not installed (install the 'llama' extra to run GGUF models)",
        )

    hw = hardware.summary()
    flags = hw.get("cpu_simd_flags") or []
    # Only enforce AVX2 when we actually have the flag list (Linux). The prebuilt
    # llama.cpp CPU wheels require AVX2; without it, loading crashes.
    if flags and "avx2" not in flags and "avx512f" not in flags:
        return CompatReport(
            Compat.NEEDS_BACKEND,
            "CPU lacks AVX2 — the prebuilt llama.cpp cannot run here",
        )

    total_ram = hw.get("memory_total")
    if size_bytes and isinstance(total_ram, int) and total_ram > 0:
        # A rough fit check: weights plus runtime/KV overhead should leave headroom.
        if size_bytes > int(total_ram * 0.9):
            return CompatReport(
                Compat.TOO_LARGE,
                "model is larger than this host's memory can comfortably hold",
            )

    if not size_bytes:
        return CompatReport(Compat.UNKNOWN, "model size not yet known")
    return CompatReport(Compat.OK, "should run on this host")
