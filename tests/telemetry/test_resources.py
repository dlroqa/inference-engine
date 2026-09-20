"""Resource sampler: well-formed snapshot, and GPU reported unavailable."""

from __future__ import annotations

from pathlib import Path

from engine.telemetry.resources import ResourceSampler, gpu


def test_snapshot_shape(tmp_path: Path) -> None:
    sampler = ResourceSampler(disk_path=tmp_path)
    snap = sampler.snapshot()
    assert set(snap) >= {"available", "cpu_percent", "memory", "process_rss", "disk"}
    assert set(snap["memory"]) == {"total", "used", "available", "percent"}
    assert set(snap["disk"]) >= {"path", "total", "used", "free", "percent"}


def test_disk_walks_up_to_existing_ancestor(tmp_path: Path) -> None:
    # A not-yet-created data dir still resolves to its containing filesystem.
    missing = tmp_path / "does" / "not" / "exist"
    sampler = ResourceSampler(disk_path=missing)
    disk = sampler.disk()
    assert disk.path == str(missing)
    # With psutil present the containing filesystem yields a real total.
    if sampler.available:
        assert disk.total is not None and disk.total > 0


def test_gpu_is_explicitly_unavailable() -> None:
    panel = gpu()
    assert panel["available"] is False
    assert "reason" in panel
