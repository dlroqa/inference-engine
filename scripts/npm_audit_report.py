#!/usr/bin/env python3
"""Summarize an ``npm audit --json`` report for CI evidence (stdlib only).

Reads the JSON npm writes (npm 7+ "auditReportVersion": 2) and prints, per
vulnerable package: severity, whether it is a direct dependency of the dashboard
or transitive (and via which package), the advisory ids/titles, the affected
range, and whether npm reports a fix within the declared semver ranges.

It distinguishes three outcomes explicitly, because they mean different things:

- ``completed`` — the audit ran; findings (possibly none) are listed.
- ``failed``    — npm could not complete the audit (e.g. registry/network error);
                  this is *not* a clean result.
- ``invalid``   — the input was not an npm audit report.

This is a reporting tool: it never changes dependencies and its exit status is
0 for ``completed`` (findings do not fail the build — the npm policy is
unchanged), and 2 for ``failed``/``invalid`` so a caller can tell them apart.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


def summarize(report: Any, package_json: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(report, dict):
        return {"status": "invalid", "reason": "not a JSON object"}
    if "error" in report:
        err = report["error"]
        summary = err.get("summary") if isinstance(err, dict) else str(err)
        return {"status": "failed", "reason": summary or "npm reported an error"}
    vulns = report.get("vulnerabilities")
    if not isinstance(vulns, dict):
        return {"status": "invalid", "reason": "no 'vulnerabilities' map (npm 7+ format)"}
    direct: set[str] = set()
    if package_json:
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            direct |= set(package_json.get(section) or {})
    rows = []
    for name, v in sorted(vulns.items()):
        advisories = []
        via_packages = []
        for via in v.get("via", []):
            if isinstance(via, dict):
                advisories.append(
                    {
                        "id": via.get("url") or via.get("source"),
                        "title": via.get("title"),
                        "severity": via.get("severity"),
                        "range": via.get("range"),
                    }
                )
            else:
                via_packages.append(str(via))
        fix = v.get("fixAvailable")
        if fix is True:
            fix_text = "yes (within declared ranges)"
        elif isinstance(fix, dict):
            major = " — semver-major" if fix.get("isSemVerMajor") else ""
            fix_text = f"via {fix.get('name')}@{fix.get('version')}{major}"
        else:
            fix_text = "no"
        rows.append(
            {
                "package": name,
                "severity": v.get("severity"),
                "direct": bool(v.get("isDirect")) or name in direct,
                "via_packages": via_packages,
                "advisories": advisories,
                "range": v.get("range"),
                "fix": fix_text,
            }
        )
    counts = (report.get("metadata") or {}).get("vulnerabilities") or {}
    return {"status": "completed", "counts": counts, "packages": rows}


def render(summary: dict[str, Any]) -> str:
    if summary["status"] != "completed":
        return f"npm audit {summary['status'].upper()}: {summary.get('reason')}\n"
    counts = summary["counts"]
    total = counts.get("total", len(summary["packages"]))
    lines = [
        "npm audit COMPLETED: "
        + ", ".join(f"{k}={counts.get(k, 0)}" for k in ("critical", "high", "moderate", "low"))
        + f", total={total}",
    ]
    if summary["packages"]:
        lines.append("")
        lines.append("| Package | Severity | Direct? | Via | Advisories | Affected | Fix |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in summary["packages"]:
            parts = [f"{a['id']} ({a['severity']}) {a['title']}" for a in r["advisories"]]
            adv = "; ".join(parts) or "—"
            via = ", ".join(r["via_packages"]) or "—"
            kind = "direct" if r["direct"] else "transitive"
            lines.append(
                f"| {r['package']} | {r['severity']} | {kind} | {via} | {adv} "
                f"| {r['range']} | {r['fix']} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="path to `npm audit --json` output")
    parser.add_argument("--package-json", default=None, help="dashboard/package.json")
    parser.add_argument("--title", default="npm audit (dashboard)")
    args = parser.parse_args(argv)
    try:
        with open(args.report, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        summary: dict[str, Any] = {"status": "invalid", "reason": f"unreadable report: {exc}"}
    else:
        pkg = None
        if args.package_json:
            with open(args.package_json, encoding="utf-8") as fh:
                pkg = json.load(fh)
        summary = summarize(report, pkg)
    text = f"### {args.title}\n\n" + render(summary)
    print(text)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0 if summary["status"] == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
