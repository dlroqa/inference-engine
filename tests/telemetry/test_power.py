"""Energy reporting: measured only with a validated probe, else unavailable."""

from __future__ import annotations

from pathlib import Path

from engine.telemetry import power
from engine.telemetry.power import PowerProbe, PowerReading, energy_state


def test_no_rapl_probe_reports_unavailable(monkeypatch, tmp_path: Path) -> None:
    # Point RAPL discovery at an empty directory: no probe available.
    monkeypatch.setattr(power, "RAPL_ROOT", tmp_path)
    probe = PowerProbe()
    assert probe.has_probe is False
    reading = probe.sample()
    assert reading.state == "unavailable"
    assert reading.watts is None
    assert "RAPL" in (reading.reason or "")


def _make_rapl(tmp_path: Path, uj: int) -> Path:
    root = tmp_path / "powercap"
    domain = root / "intel-rapl:0"
    domain.mkdir(parents=True)
    (domain / "energy_uj").write_text(str(uj))
    return root


def test_rapl_baseline_then_measured(monkeypatch, tmp_path: Path) -> None:
    root = _make_rapl(tmp_path, 1_000_000)
    monkeypatch.setattr(power, "RAPL_ROOT", root)
    probe = PowerProbe()
    assert probe.has_probe is True

    # First sample establishes the baseline -> still unavailable.
    first = probe.sample(now=100.0)
    assert first.state == "unavailable"
    assert first.reason == "establishing baseline"

    # Advance the counter by 2 J over 1 s -> 2 W measured.
    (root / "intel-rapl:0" / "energy_uj").write_text(str(1_000_000 + 2_000_000))
    second = probe.sample(now=101.0)
    assert second.state == "measured"
    assert second.watts is not None
    assert abs(second.watts - 2.0) < 1e-6
    assert second.source == "rapl"


def test_counter_wrap_is_not_fabricated(monkeypatch, tmp_path: Path) -> None:
    root = _make_rapl(tmp_path, 5_000_000)
    monkeypatch.setattr(power, "RAPL_ROOT", root)
    probe = PowerProbe()
    probe.sample(now=0.0)  # baseline
    # Counter goes backwards (wrap): we must not invent a negative/huge wattage.
    (root / "intel-rapl:0" / "energy_uj").write_text("10")
    reading = probe.sample(now=1.0)
    assert reading.state == "unavailable"
    assert reading.watts is None


def test_energy_state_derives_j_per_token_only_when_measured() -> None:
    measured = PowerReading(state="measured", watts=10.0, source="rapl")
    panel = energy_state(measured, completion_tokens_per_s=5.0)
    assert panel["state"] == "measured"
    assert panel["watts"] == 10.0
    assert panel["j_per_token"] == 2.0  # 10 W / 5 tok/s
    assert panel["tokens_per_joule"] == 0.5

    # No throughput -> measured power but no per-token figure.
    panel = energy_state(measured, completion_tokens_per_s=None)
    assert panel["state"] == "measured"
    assert panel["j_per_token"] is None


def test_energy_state_unavailable_never_estimates() -> None:
    unavailable = PowerReading(state="unavailable", reason="no probe")
    panel = energy_state(unavailable, completion_tokens_per_s=10.0)
    assert panel["state"] == "unavailable"
    assert panel["watts"] is None
    assert panel["j_per_token"] is None
    assert panel["tokens_per_joule"] is None
