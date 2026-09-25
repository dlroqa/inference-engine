"""Energy measurement with an explicit *measured* vs *unavailable* state.

Per the aligned build direction, energy-per-token is reported as **measured**
only where a validated platform probe exists. Right now that probe is Intel/AMD
**RAPL** via the Linux ``powercap`` sysfs interface (``/sys/class/powercap``),
which exposes a monotonically increasing microjoule counter per domain. Power (W)
is derived from the counter delta over the sample interval.

When no validated probe is readable (no RAPL, permission denied, non-Linux, or a
counter wrap we can't trust), the state is ``"unavailable"`` with a reason — we do
**not** fall back to TDP-derived estimates and present them as measured energy.
That honesty is the whole point of this block's energy handling.

Energy-per-token (J/tok) and tokens/joule are derived only from measured power and
observed completion-token throughput; with no measured power they are ``None`` and
the state is ``"unavailable"``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

RAPL_ROOT = Path("/sys/class/powercap")
# 2^32 µJ is the documented max_energy_range for many domains; a delta larger than
# a full 64-bit range is implausible, but we guard wrap by rejecting negatives.


@dataclass(slots=True)
class PowerReading:
    """A single power sample.

    ``state`` is ``"measured"`` or ``"unavailable"``. ``watts`` is populated only
    when measured. ``source`` names the probe (``"rapl"``) or is ``None``.
    """

    state: str
    watts: float | None = None
    source: str | None = None
    reason: str | None = None

    @property
    def measured(self) -> bool:
        return self.state == "measured"


def _read_rapl_domains() -> list[Path]:
    """Return the RAPL package energy-counter files, if readable."""
    if not RAPL_ROOT.exists():
        return []
    domains: list[Path] = []
    try:
        for entry in sorted(RAPL_ROOT.glob("intel-rapl:*")):
            energy = entry / "energy_uj"
            if energy.is_file():
                domains.append(energy)
    except OSError:
        return []
    return domains


def _read_uj(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


class PowerProbe:
    """Samples total system package power via RAPL counter deltas.

    Construct once and call :meth:`sample` on each metrics tick. The first sample
    after construction (or after a gap) establishes the baseline and reports
    ``unavailable`` until a delta over a known interval is available.
    """

    def __init__(self) -> None:
        self._domains = _read_rapl_domains()
        # Left unset so the first ``sample`` establishes the baseline against the
        # caller-provided (or real) clock rather than a construction-time one.
        self._last_total_uj: int | None = None
        self._last_ts: float | None = None

    @property
    def has_probe(self) -> bool:
        return bool(self._domains)

    def _read_total(self) -> int | None:
        if not self._domains:
            return None
        total = 0
        read_any = False
        for path in self._domains:
            val = _read_uj(path)
            if val is None:
                # A domain became unreadable (e.g. permissions): treat as no probe.
                return None
            total += val
            read_any = True
        return total if read_any else None

    def sample(self, now: float | None = None) -> PowerReading:
        if not self._domains:
            return PowerReading(
                state="unavailable",
                reason="no RAPL powercap interface (needs Linux Intel/AMD RAPL)",
            )
        now = now if now is not None else time.monotonic()
        current = self._read_total()
        if current is None:
            self._last_total_uj = None
            self._last_ts = None
            return PowerReading(
                state="unavailable",
                reason="RAPL energy counter unreadable (permissions?)",
            )
        if self._last_total_uj is None or self._last_ts is None:
            self._last_total_uj = current
            self._last_ts = now
            return PowerReading(
                state="unavailable",
                source="rapl",
                reason="establishing baseline",
            )
        dt = now - self._last_ts
        delta_uj = current - self._last_total_uj
        self._last_total_uj = current
        self._last_ts = now
        if dt <= 0 or delta_uj < 0:
            # Counter wrap or clock anomaly: don't fabricate a number.
            return PowerReading(
                state="unavailable", source="rapl", reason="counter wrap; resampling"
            )
        watts = (delta_uj / 1_000_000.0) / dt
        return PowerReading(state="measured", watts=watts, source="rapl")


def energy_state(reading: PowerReading, completion_tokens_per_s: float | None) -> dict[str, object]:
    """Derive the energy panel from a power reading and token throughput.

    J/tok and tokens/J are produced only from *measured* power; otherwise the
    panel is explicitly ``unavailable`` (never a TDP estimate presented as real).
    """
    if not reading.measured or reading.watts is None:
        return {
            "state": "unavailable",
            "watts": None,
            "j_per_token": None,
            "tokens_per_joule": None,
            "source": reading.source,
            "reason": reading.reason or "no validated power probe",
        }
    panel: dict[str, object] = {
        "state": "measured",
        "watts": round(reading.watts, 3),
        "source": reading.source,
        "j_per_token": None,
        "tokens_per_joule": None,
    }
    if completion_tokens_per_s and completion_tokens_per_s > 0:
        j_per_token = reading.watts / completion_tokens_per_s
        panel["j_per_token"] = round(j_per_token, 6)
        panel["tokens_per_joule"] = round(1.0 / j_per_token, 3) if j_per_token > 0 else None
    return panel
