#!/usr/bin/env python3
"""Print the CI runner's resource inventory (stdlib only, no secrets).

Each CI job records what it actually ran on — OS/version, native architecture,
CPU model/features/cores, RAM, free disk, and toolchain versions — so acceptance
evidence names the hardware behind it (BUILD_EXECUTION_GUIDANCE.md). Output goes
to stdout (the job log) and, when available, the GitHub step summary.

It never prints environment variables, tokens, hostnames, or user names.

Usage::

    python scripts/runner_inventory.py --job "Lint, type-check, test & coverage"
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys

_SIMD = ("avx512f", "avx2", "avx", "fma", "f16c", "sse4_2", "neon", "asimd", "sve")


def _run(*cmd: str) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def _cpu_model_and_flags() -> tuple[str, list[str]]:
    system = platform.system()
    if system == "Linux":
        model, flags = "", set()
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as fh:
                for line in fh:
                    key, _, value = line.partition(":")
                    key = key.strip().lower()
                    if key in ("model name", "hardware", "cpu model") and not model:
                        model = value.strip()
                    elif key in ("flags", "features"):
                        flags |= set(value.split())
        except OSError:
            pass
        return model or platform.processor() or "unknown", [f for f in _SIMD if f in flags]
    if system == "Darwin":
        model = _run("sysctl", "-n", "machdep.cpu.brand_string") or platform.processor()
        feats = " ".join(
            _run("sysctl", "-n", key)
            for key in ("machdep.cpu.features", "machdep.cpu.leaf7_features")
        ).lower()
        feats = feats.replace("avx1.0", "avx")  # macOS spells AVX as AVX1.0
        flags = [f for f in _SIMD if re.search(rf"\b{f.replace('_', '.')}\b", feats)]
        if platform.machine() == "arm64":
            flags.append("neon")
        return model or "unknown", flags
    if system == "Windows":
        model = _run(
            "powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"
        )
        return model or platform.processor() or "unknown", []
    return platform.processor() or "unknown", []


def _ram_bytes() -> int | None:
    system = platform.system()
    try:
        if system == "Linux":
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) * 1024
        elif system == "Darwin":
            return int(_run("sysctl", "-n", "hw.memsize") or 0) or None
        elif system == "Windows":
            value = _run(
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
            )
            return int(value) if value.isdigit() else None
    except (OSError, ValueError):
        return None
    return None


def _gib(n: int | None) -> str:
    return f"{n / 2**30:.1f} GiB" if n else "unknown"


def _tool(name: str, *args: str) -> str | None:
    if shutil.which(name) is None:
        return None
    first = _run(name, *args).splitlines()
    return first[0] if first else None


def inventory() -> list[tuple[str, str]]:
    model, flags = _cpu_model_and_flags()
    disk = shutil.disk_usage(os.getcwd())
    rows = [
        ("OS", f"{platform.system()} {platform.release()} ({platform.platform(terse=True)})"),
        ("Native arch", platform.machine() or "unknown"),
        ("CPU", model),
        ("Cores (logical)", str(os.cpu_count() or "unknown")),
        ("SIMD", " ".join(flags) or "not reported on this OS"),
        ("RAM", _gib(_ram_bytes())),
        ("Disk free (workspace)", _gib(disk.free)),
        ("Python", sys.version.split()[0]),
    ]
    for label, name, args in (
        ("Node", "node", ("--version",)),
        ("npm", "npm", ("--version",)),
        ("Docker", "docker", ("--version",)),
        ("C compiler", "cc", ("--version",)),
    ):
        version = _tool(name, *args)
        if version:
            rows.append((label, version))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, help="human-readable job name")
    args = parser.parse_args(argv)
    lines = [f"### Runner inventory — {args.job}"] + [f"- {k}: {v}" for k, v in inventory()]
    text = "\n".join(lines) + "\n"
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
