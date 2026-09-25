"""Release tooling for the prebuilt CPU image (see docs/releasing.md).

Stdlib-only so it runs on a bare CI runner and inside the Docker build stage.
Every subcommand fails closed (non-zero exit with an ``error:`` line) so the
release workflow stops before anything is built or published.

Subcommands:
    check-version   Validate the vX.Y.Z tag against the single version source.
    stable-tags     Print the image tags a stable release may move.
    verify-wheel    Assert the wheel bundles the dashboard SPA and has the version.
    oci-digests     Print the manifest + config digests of the candidate OCI archive.
    saved-config    Print the config digest of a ``docker save`` stream (tested image).
    render-compose  Pin deploy/compose.yaml to an immutable @sha256 image.
    scan-gate       Apply vulnerability exceptions to a grype report; fail on critical.
    notes           Render the release notes (digest, platform, upgrade/rollback).
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
DIGEST_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
COMPOSE_IMAGE_RE = re.compile(r"image: \$\{IMAGE[^}\n]*\}")
SEVERITIES = ("Critical", "High", "Medium", "Low", "Negligible", "Unknown")


class ReleaseError(Exception):
    """A release precondition failed; the pipeline must stop."""


def _vtuple(version: str) -> tuple[int, int, int]:
    m = VERSION_RE.match(version)
    if not m:
        raise ReleaseError(f"not a MAJOR.MINOR.PATCH version: {version!r}")
    return int(m[1]), int(m[2]), int(m[3])


def parse_tag(tag: str) -> str:
    """``v1.2.3`` -> ``1.2.3``; anything else (branches, pre-releases) is rejected."""
    if not TAG_RE.match(tag):
        raise ReleaseError(f"release tag must be vMAJOR.MINOR.PATCH, got {tag!r}")
    return tag[1:]


def source_version(root: Path = ROOT) -> str:
    """Read ``engine.__version__`` statically (no import, so no deps needed)."""
    tree = ast.parse((root / "engine" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    raise ReleaseError("engine/__init__.py does not define a literal __version__")


def changelog_section(version: str, root: Path = ROOT) -> str:
    """Body of the ``## [X.Y.Z]`` section in CHANGELOG.md (required for a release)."""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    if not m or not m.group(1).strip():
        raise ReleaseError(f"CHANGELOG.md has no non-empty '## [{version}]' section")
    return m.group(1).strip()


def check_version(tag: str | None, root: Path = ROOT) -> str:
    """Return the release version, failing if tag, source, and changelog disagree.

    With no tag (a workflow_dispatch dry run) the source version is validated and
    used as-is; a moving branch name is never a version.
    """
    src = source_version(root)
    _vtuple(src)
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    if 'dynamic = ["version"]' not in pyproject or "engine.__version__" not in pyproject:
        raise ReleaseError("pyproject.toml must take its version from engine.__version__")
    if tag is not None:
        tagged = parse_tag(tag)
        if tagged != src:
            raise ReleaseError(f"tag {tag} does not match engine.__version__ {src}")
    changelog_section(src, root)
    return src


def stable_tags(version: str, existing: list[str]) -> list[str]:
    """Tags to apply for ``version``: always ``vX.Y.Z``; ``X.Y``/``X``/``latest``
    only when this release is the highest in that line, so a patch to an older
    line never moves a newer convenience tag backwards."""
    mine = _vtuple(version)
    others = [_vtuple(parse_tag(t)) for t in existing if TAG_RE.match(t)]
    others = [o for o in others if o != mine]
    tags = [f"v{version}"]
    if all(o <= mine for o in others if o[:2] == mine[:2]):
        tags.append(f"{mine[0]}.{mine[1]}")
    if all(o <= mine for o in others if o[0] == mine[0]):
        tags.append(f"{mine[0]}")
    if all(o <= mine for o in others):
        tags.append("latest")
    return tags


def verify_wheel(path: Path, expect_version: str | None = None) -> list[str]:
    """Assert the wheel ships the dashboard SPA (index + emitted assets)."""
    if expect_version is not None and f"-{expect_version}-" not in path.name:
        raise ReleaseError(f"wheel {path.name} is not version {expect_version}")
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    if "engine/static/index.html" not in names:
        raise ReleaseError(f"{path.name} is missing engine/static/index.html")
    assets = [n for n in names if n.startswith("engine/static/assets/") and not n.endswith("/")]
    if not assets:
        raise ReleaseError(f"{path.name} is missing the engine/static/assets/ bundle")
    return assets


OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def oci_digests(archive: Path) -> dict[str, str]:
    """Digests of the single linux/amd64 image in an OCI archive (buildx ``type=oci``).

    The manifest digest is what the registry will serve after a digest-preserving
    push; the config digest identifies the image content (the docker image ID)."""
    with tarfile.open(archive) as tf:

        def blob(digest: str) -> dict[str, Any]:
            member = tf.extractfile(f"blobs/sha256/{digest.split(':', 1)[1]}")
            if member is None:
                raise ReleaseError(f"blob {digest} missing from {archive.name}")
            return dict(json.load(member))

        index_file = tf.extractfile("index.json")
        if index_file is None:
            raise ReleaseError(f"{archive.name} has no index.json")
        manifests = json.load(index_file).get("manifests", [])
        if len(manifests) != 1 or manifests[0].get("mediaType") != OCI_MANIFEST:
            raise ReleaseError("candidate must be exactly one single-platform OCI image manifest")
        manifest_digest = manifests[0]["digest"]
        config_digest = blob(manifest_digest)["config"]["digest"]
        config = blob(config_digest)
    platform = f"{config.get('os')}/{config.get('architecture')}"
    if platform != "linux/amd64":
        raise ReleaseError(f"candidate platform is {platform}, expected linux/amd64")
    return {"manifest": manifest_digest, "config": config_digest, "platform": platform}


def saved_config_digest(stream: Any) -> str:
    """Config digest of the image in a ``docker save`` tar stream (classic or
    containerd image store), used to prove the tested image is the candidate."""
    with tarfile.open(fileobj=stream, mode="r|*") as tf:
        for member in tf:
            if member.name == "manifest.json":
                f = tf.extractfile(member)
                if f is None:
                    break
                entries = json.load(f)
                if len(entries) != 1:
                    raise ReleaseError("docker save stream must contain exactly one image")
                name = Path(entries[0]["Config"]).name.removesuffix(".json")
                return f"sha256:{name}"
    raise ReleaseError("docker save stream has no manifest.json")


def render_compose(template: str, image_ref: str) -> str:
    """Replace the ``${IMAGE...}`` placeholder with an immutable digest reference."""
    if not DIGEST_REF_RE.match(image_ref):
        raise ReleaseError(f"image must be pinned as <repo>@sha256:<digest>, got {image_ref!r}")
    rendered, n = COMPOSE_IMAGE_RE.subn(f"image: {image_ref}", template)
    if n != 1:
        raise ReleaseError(f"expected exactly one ${{IMAGE}} placeholder, found {n}")
    return rendered


def load_exceptions(path: Path, today: dt.date) -> list[dict[str, Any]]:
    """Documented, time-bounded vulnerability exceptions. Expired or undocumented
    entries are an error, never silently ignored."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for entry in data.get("exceptions", []):
        missing = [k for k in ("id", "package", "reason", "expires") if not entry.get(k)]
        if missing:
            raise ReleaseError(f"vulnerability exception {entry!r} is missing {missing}")
        if dt.date.fromisoformat(entry["expires"]) < today:
            raise ReleaseError(
                f"vulnerability exception {entry['id']} ({entry['package']}) expired "
                f"on {entry['expires']}; fix it or renew it with a new justification"
            )
        out.append(entry)
    return out


def scan_gate(
    report: dict[str, Any], exceptions: list[dict[str, Any]]
) -> tuple[dict[str, int], list[dict[str, str]], list[dict[str, Any]]]:
    """Count grype matches by severity and return the blocking critical findings
    (critical and not covered by an exception) plus the exceptions applied."""
    counts = {s: 0 for s in SEVERITIES}
    blocking: list[dict[str, str]] = []
    applied: list[dict[str, Any]] = []
    for match in report.get("matches", []):
        vuln = match.get("vulnerability", {})
        art = match.get("artifact", {})
        sev = vuln.get("severity", "Unknown")
        counts[sev if sev in counts else "Unknown"] += 1
        if sev != "Critical":
            continue
        finding = {
            "id": vuln.get("id", "?"),
            "package": art.get("name", "?"),
            "version": art.get("version", "?"),
        }
        exc = next(
            (
                e
                for e in exceptions
                if e["id"] == finding["id"] and e["package"] == finding["package"]
            ),
            None,
        )
        if exc is None:
            blocking.append(finding)
        elif exc not in applied:
            applied.append(exc)
    return counts, blocking, applied


def scan_markdown(
    counts: dict[str, int], blocking: list[dict[str, str]], applied: list[dict[str, Any]]
) -> str:
    lines = [
        "| Severity | Findings |",
        "| --- | --- |",
        *(f"| {s} | {counts[s]} |" for s in SEVERITIES),
        "",
    ]
    if blocking:
        lines.append("**Blocking critical findings:**")
        lines += [f"- {b['id']} in {b['package']} {b['version']}" for b in blocking]
    else:
        lines.append("No unexcepted critical vulnerabilities.")
    if applied:
        lines += ["", "**Time-bounded exceptions applied:**"]
        lines += [
            f"- {e['id']} in {e['package']} — {e['reason']} (expires {e['expires']})"
            for e in applied
        ]
    return "\n".join(lines)


def release_notes(
    *,
    version: str,
    commit: str,
    created: str,
    image_repo: str,
    digest: str,
    changes: str,
    scan_summary: str,
    tags: list[str],
    provenance_url: str,
    sbom_url: str,
) -> str:
    image_ref = f"{image_repo}@{digest}"
    if not DIGEST_REF_RE.match(image_ref):
        raise ReleaseError(f"invalid immutable image reference {image_ref!r}")
    tag_list = ", ".join(f"`{t}`" for t in tags)
    return f"""\
# Inference Engine v{version}

**Immutable image (deploy this):**

```text
{image_ref}
```

| | |
| --- | --- |
| Version | `{version}` |
| Commit | `{commit}` |
| Build date (UTC) | `{created}` |
| Digest | `{digest}` |
| Tags applied to this digest | {tag_list} |
| Platform | `linux/amd64` (CPU, llama.cpp backend included) |
| Provenance | {provenance_url} |
| SBOM attestation | {sbom_url} |

## Support boundary

- **Supported:** Linux x86-64 hosts with **AVX2**, Docker Engine + the Docker Compose plugin.
- **Not supported in this release:** macOS, Windows, ARM, and any GPU (CUDA/ROCm)
  deployment. No GPU acceleration is claimed.
- Models are **not** bundled. Place GGUF files under `/data/models` (host mount) or
  download them through the authenticated dashboard **Models** page.

## Changes

{changes}

## Install

One-command bootstrap (the installer verifies `compose.yaml` and
`inference-engine.env.example` against this release's `SHA256SUMS`):

```bash
curl --fail --location --proto '=https' --tlsv1.2 \\
  https://github.com/dlroqa/inference-engine/releases/download/v{version}/install.sh \\
  | bash -s -- v{version}
```

For a separately verified installer download, download `install.sh` and `SHA256SUMS`,
verify `install.sh` with `sha256sum --strict --check --ignore-missing SHA256SUMS`, then
run `bash install.sh v{version}`. To install manually, download `compose.yaml` and
`inference-engine.env.example` from this release, then:

```bash
cp inference-engine.env.example inference-engine.env   # keep secrets out of git
docker compose -f compose.yaml pull
docker compose -f compose.yaml up -d
docker compose -f compose.yaml exec inference-engine \\
  inference-engine keys create --label owner
```

The last command prints the owner key **once** — save it before opening
`http://127.0.0.1:8000/dashboard`. Put the service behind TLS (Caddy, nginx,
Traefik) or a private network boundary before exposing it publicly. Full guide:
`docs/deployment.md`.

## Upgrade

1. Back up state: `docker compose exec inference-engine inference-engine backup --out /data/backups`
   and separately back up `/data/models` (model files are excluded from backups).
2. Set the image to `{image_ref}`
   (use this release's `compose.yaml`, or `export IMAGE=...` with `deploy/compose.yaml`).
3. `docker compose pull && docker compose up -d`.
4. Verify `/version` reports `{version}`, then `/healthz`, `/readyz`, `/dashboard`,
   and a small generation.

## Rollback

1. `docker compose stop`.
2. Pin the previous known-good release digest from that release's notes.
3. Database migrations are forward-only. If this release applied a new migration,
   restore the pre-upgrade backup with
   `inference-engine restore --from <archive> --force` before starting the old image.
4. `docker compose up -d`, then verify `/healthz` and a small generation.

## Verify this artifact

```bash
gh attestation verify oci://{image_ref} --repo dlroqa/inference-engine
```

## Vulnerability scan (grype, fail on critical)

{scan_summary}
"""


def _cmd(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    cv = sub.add_parser("check-version")
    cv.add_argument("--tag", default=None, help="Release tag (omit for a dry run).")

    st = sub.add_parser("stable-tags")
    st.add_argument("--version", required=True)
    st.add_argument("existing", nargs="*", help="Existing git tags.")

    vw = sub.add_parser("verify-wheel")
    vw.add_argument("wheel", type=Path)
    vw.add_argument("--expect-version", default=None)

    od = sub.add_parser("oci-digests")
    od.add_argument("archive", type=Path)

    sub.add_parser("saved-config").add_argument("stream", help="docker save tar, or - for stdin")

    rc = sub.add_parser("render-compose")
    rc.add_argument("--image", required=True, help="<repo>@sha256:<digest>")
    rc.add_argument("--template", type=Path, default=ROOT / "deploy" / "compose.yaml")
    rc.add_argument("--out", type=Path, required=True)

    sg = sub.add_parser("scan-gate")
    sg.add_argument("--report", type=Path, required=True, help="grype JSON report")
    sg.add_argument(
        "--exceptions", type=Path, default=ROOT / "deploy" / "vulnerability-exceptions.json"
    )
    sg.add_argument("--summary-out", type=Path, required=True)

    nt = sub.add_parser("notes")
    for flag in ("--version", "--commit", "--created", "--image-repo", "--digest"):
        nt.add_argument(flag, required=True)
    nt.add_argument("--scan-summary", type=Path, required=True)
    nt.add_argument("--tags", nargs="+", required=True)
    nt.add_argument("--provenance-url", default="(dry run — not attested)")
    nt.add_argument("--sbom-url", default="(dry run — not attested)")
    nt.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "check-version":
            print(check_version(args.tag))
        elif args.cmd == "stable-tags":
            print(" ".join(stable_tags(args.version, args.existing)))
        elif args.cmd == "verify-wheel":
            assets = verify_wheel(args.wheel, args.expect_version)
            print(f"{args.wheel.name}: dashboard index + {len(assets)} asset file(s) present")
        elif args.cmd == "oci-digests":
            print(json.dumps(oci_digests(args.archive)))
        elif args.cmd == "saved-config":
            if args.stream == "-":
                print(saved_config_digest(sys.stdin.buffer))
            else:
                with open(args.stream, "rb") as fh:
                    print(saved_config_digest(fh))
        elif args.cmd == "render-compose":
            template = args.template.read_text(encoding="utf-8")
            args.out.write_text(render_compose(template, args.image), encoding="utf-8")
        elif args.cmd == "scan-gate":
            report = json.loads(args.report.read_text(encoding="utf-8"))
            exceptions = load_exceptions(args.exceptions, dt.datetime.now(dt.UTC).date())
            counts, blocking, applied = scan_gate(report, exceptions)
            summary = scan_markdown(counts, blocking, applied)
            args.summary_out.write_text(summary + "\n", encoding="utf-8")
            print(summary)
            if blocking:
                raise ReleaseError(f"{len(blocking)} critical vulnerabilit(y/ies) block release")
        elif args.cmd == "notes":
            notes = release_notes(
                version=args.version,
                commit=args.commit,
                created=args.created,
                image_repo=args.image_repo,
                digest=args.digest,
                changes=changelog_section(args.version),
                scan_summary=args.scan_summary.read_text(encoding="utf-8").strip(),
                tags=args.tags,
                provenance_url=args.provenance_url,
                sbom_url=args.sbom_url,
            )
            args.out.write_text(notes, encoding="utf-8")
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cmd())
