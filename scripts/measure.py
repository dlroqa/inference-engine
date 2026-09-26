#!/usr/bin/env python3
"""Lightweight, honestly-labelled resource measurement for CI steps (stdlib).

Three modes, each reporting a *different* quantity — they are never mixed:

``run --label L -- CMD...``
    Runs CMD (stdio inherited, exit status propagated unchanged) and reports
    wall time plus ``ru_maxrss`` from ``getrusage(RUSAGE_CHILDREN)``: the peak
    resident set of the **largest single descendant process**, not the sum of
    the process tree. Linux and macOS only (units normalized to MiB).

``pid --label L PID``
    Reads the kernel's high-water mark ``VmHWM`` from ``/proc/PID/status`` for a
    long-running process (e.g. the engine server) — that process's true peak
    RSS, excluding its children. Linux only.

``cgroup --label L --container NAME``
    Reads ``memory.peak`` of a running Docker container's cgroup (cgroup v2):
    the peak memory charged to the **whole container** (all processes, including
    page cache). Linux only.

Unavailable data is reported as ``unmeasured`` with a reason, never guessed.
Results go to stdout and the GitHub step summary.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time

MIB = 1024 * 1024


def _emit(label: str, fields: dict[str, str]) -> None:
    line = f"MEASURE {label}: " + ", ".join(f"{k}={v}" for k, v in fields.items())
    print(line, flush=True)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"- `{label}`: " + ", ".join(f"{k} {v}" for k, v in fields.items()) + "\n")


def maxrss_to_mib(ru_maxrss: int, system: str) -> float:
    # Linux reports KiB; macOS reports bytes.
    return ru_maxrss / MIB if system == "Darwin" else ru_maxrss / 1024


def parse_vmhwm(status_text: str) -> float | None:
    """VmHWM in MiB from /proc/PID/status text, or None if absent."""
    for line in status_text.splitlines():
        if line.startswith("VmHWM:"):
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "kB":
                return int(parts[1]) / 1024
    return None


def cmd_run(label: str, command: list[str]) -> int:
    if not command:
        print("measure run: no command given", file=sys.stderr)
        return 2
    start = time.monotonic()
    proc = subprocess.run(command, check=False)
    wall = time.monotonic() - start
    fields = {"wall_s": f"{wall:.1f}", "exit": str(proc.returncode)}
    system = platform.system()
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        fields["max_rss_largest_process_MiB"] = f"{maxrss_to_mib(usage.ru_maxrss, system):.0f}"
    except (ImportError, OSError):
        fields["max_rss"] = f"unmeasured (getrusage unavailable on {system})"
    _emit(label, fields)
    return proc.returncode


def cmd_pid(label: str, pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            hwm = parse_vmhwm(fh.read())
    except OSError as exc:
        _emit(label, {"peak_rss_process": f"unmeasured ({exc.__class__.__name__})"})
        return 0
    value = f"{hwm:.0f}" if hwm is not None else "unmeasured (no VmHWM)"
    _emit(label, {"peak_rss_process_only_MiB (VmHWM)": value})
    return 0


def cmd_cgroup(label: str, container: str) -> int:
    out = subprocess.run(
        ["docker", "exec", container, "cat", "/sys/fs/cgroup/memory.peak"],
        capture_output=True,
        text=True,
        check=False,
    )
    raw = out.stdout.strip()
    if out.returncode != 0 or not raw.isdigit():
        _emit(label, {"container_peak": "unmeasured (memory.peak not readable; cgroup v1?)"})
        return 0
    _emit(label, {"container_cgroup_peak_MiB (memory.peak)": f"{int(raw) / MIB:.0f}"})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    run = sub.add_parser("run")
    run.add_argument("--label", required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    pid = sub.add_parser("pid")
    pid.add_argument("--label", required=True)
    pid.add_argument("pid", type=int)
    cg = sub.add_parser("cgroup")
    cg.add_argument("--label", required=True)
    cg.add_argument("--container", required=True)
    args = parser.parse_args(argv)
    if args.mode == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        return cmd_run(args.label, command)
    if args.mode == "pid":
        return cmd_pid(args.label, args.pid)
    return cmd_cgroup(args.label, args.container)


if __name__ == "__main__":
    sys.exit(main())
