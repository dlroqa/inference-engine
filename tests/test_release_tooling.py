"""Tests for scripts/release.py — the fail-closed release gates."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import io
import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

import engine

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_tool", ROOT / "scripts" / "release.py")
assert _spec and _spec.loader
release = importlib.util.module_from_spec(_spec)
sys.modules["release_tool"] = release
_spec.loader.exec_module(release)
_e2e_spec = importlib.util.spec_from_file_location(
    "release_image_e2e", ROOT / "scripts" / "release_image_e2e.py"
)
assert _e2e_spec and _e2e_spec.loader
e2e = importlib.util.module_from_spec(_e2e_spec)
sys.modules["release_image_e2e"] = e2e
_e2e_spec.loader.exec_module(e2e)

DIGEST = "sha256:" + "a" * 64
REPO = "ghcr.io/dlroqa/inference-engine"


def _tree(tmp_path: Path, version: str = "1.2.3", changelog: str | None = None) -> Path:
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (tmp_path / "pyproject.toml").write_text(
        'dynamic = ["version"]\nversion = { attr = "engine.__version__" }\n'
    )
    if changelog is None:
        changelog = f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - 2026-01-01\n\n- Thing.\n"
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return tmp_path


# --- version truth ------------------------------------------------------------


@pytest.mark.parametrize("tag", ["v1.2", "1.2.3", "v1.2.3-rc1", "v01.2.3", "main", "vX.Y.Z"])
def test_parse_tag_rejects_non_release_tags(tag: str) -> None:
    with pytest.raises(release.ReleaseError):
        release.parse_tag(tag)


def test_parse_tag_strips_v() -> None:
    assert release.parse_tag("v10.0.7") == "10.0.7"


def test_repo_version_is_consistent() -> None:
    # The real checkout: engine.__version__, pyproject dynamic metadata, CHANGELOG.
    assert release.check_version(None) == engine.__version__
    assert release.check_version(f"v{engine.__version__}") == engine.__version__


def test_check_version_matching_tag(tmp_path: Path) -> None:
    assert release.check_version("v1.2.3", _tree(tmp_path)) == "1.2.3"


def test_check_version_mismatched_tag_fails(tmp_path: Path) -> None:
    with pytest.raises(release.ReleaseError, match="does not match"):
        release.check_version("v1.2.4", _tree(tmp_path))


def test_check_version_requires_changelog_section(tmp_path: Path) -> None:
    root = _tree(tmp_path, changelog="# Changelog\n\n## [1.2.3]\n\n## [1.2.2]\n- old\n")
    with pytest.raises(release.ReleaseError, match="CHANGELOG"):
        release.check_version("v1.2.3", root)


def test_check_version_rejects_static_pyproject_version(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "pyproject.toml").write_text('version = "1.2.3"\n')
    with pytest.raises(release.ReleaseError, match="pyproject"):
        release.check_version(None, root)


def test_check_version_rejects_placeholder_like_versions(tmp_path: Path) -> None:
    with pytest.raises(release.ReleaseError):
        release.check_version(None, _tree(tmp_path, version="0.1.0.dev0"))


def test_changelog_section_extracts_only_that_version(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        changelog="## [2.0.0]\n\n- new\n\n## [1.0.0]\n\n- old\n",
        version="2.0.0",
    )
    assert release.changelog_section("2.0.0", root) == "- new"


# --- stable tags ---------------------------------------------------------------


def test_stable_tags_first_release() -> None:
    assert release.stable_tags("0.1.0", []) == ["v0.1.0", "0.1", "0", "latest"]


def test_stable_tags_ignore_the_release_itself_and_non_release_tags() -> None:
    assert release.stable_tags("0.1.0", ["v0.1.0", "block-9", "v0.1.0-rc1"]) == [
        "v0.1.0",
        "0.1",
        "0",
        "latest",
    ]


def test_stable_tags_backport_does_not_move_newer_pointers() -> None:
    existing = ["v1.0.0", "v1.1.0", "v2.0.0"]
    assert release.stable_tags("1.0.1", existing) == ["v1.0.1", "1.0"]
    assert release.stable_tags("1.1.1", existing) == ["v1.1.1", "1.1", "1"]
    assert release.stable_tags("2.0.1", existing) == ["v2.0.1", "2.0", "2", "latest"]


# --- wheel verification --------------------------------------------------------


def _wheel(tmp_path: Path, names: list[str], version: str = "1.2.3") -> Path:
    path = tmp_path / f"inference_engine-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for n in names:
            zf.writestr(n, "x")
    return path


def test_verify_wheel_accepts_bundled_dashboard(tmp_path: Path) -> None:
    whl = _wheel(tmp_path, ["engine/static/index.html", "engine/static/assets/app.js"])
    assert release.verify_wheel(whl, "1.2.3") == ["engine/static/assets/app.js"]


@pytest.mark.parametrize(
    "names",
    [["engine/static/assets/app.js"], ["engine/static/index.html"], ["engine/__init__.py"]],
)
def test_verify_wheel_rejects_missing_dashboard(tmp_path: Path, names: list[str]) -> None:
    with pytest.raises(release.ReleaseError):
        release.verify_wheel(_wheel(tmp_path, names))


def test_verify_wheel_rejects_wrong_version(tmp_path: Path) -> None:
    whl = _wheel(tmp_path, ["engine/static/index.html", "engine/static/assets/a.js"], "0.0.0")
    with pytest.raises(release.ReleaseError, match="not version"):
        release.verify_wheel(whl, "1.2.3")


# --- compose rendering ---------------------------------------------------------


def test_render_compose_pins_digest_in_repo_template() -> None:
    template = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    rendered = release.render_compose(template, f"{REPO}@{DIGEST}")
    assert f"image: {REPO}@{DIGEST}" in rendered
    assert "${IMAGE" not in rendered
    # The mandatory deployment properties survive rendering.
    for needle in (
        '"127.0.0.1:8000:8000"',
        "engine-data:/data",
        'IE_REQUIRE_AUTH: "true"',
        'IE_REQUIRE_MODEL_READY: "true"',
        'IE_ALLOW_NETWORK_BIND: "true"',
        "stop_grace_period: 40s",
    ):
        assert needle in rendered


def test_compose_grace_period_exceeds_drain_timeout() -> None:
    text = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    drain = float(text.split('IE_DRAIN_TIMEOUT_S: "')[1].split('"')[0])
    grace = float(text.split("stop_grace_period: ")[1].split("s")[0])
    assert grace > drain


@pytest.mark.parametrize("ref", [f"{REPO}:v1.0.0", f"{REPO}@sha256:abc", "latest"])
def test_render_compose_rejects_mutable_refs(ref: str) -> None:
    with pytest.raises(release.ReleaseError):
        release.render_compose("image: ${IMAGE:?x}\n", ref)


def test_render_compose_requires_single_placeholder() -> None:
    with pytest.raises(release.ReleaseError, match="placeholder"):
        release.render_compose("image: foo\n", f"{REPO}@{DIGEST}")


# --- vulnerability gate --------------------------------------------------------


def _match(vid: str, sev: str, pkg: str = "zlib") -> dict:
    return {
        "vulnerability": {"id": vid, "severity": sev},
        "artifact": {"name": pkg, "version": "1"},
    }


def test_scan_gate_blocks_on_critical() -> None:
    report = {"matches": [_match("CVE-1", "Critical"), _match("CVE-2", "High")]}
    counts, blocking, applied = release.scan_gate(report, [])
    assert counts["Critical"] == 1 and counts["High"] == 1
    assert blocking == [{"id": "CVE-1", "package": "zlib", "version": "1"}]
    assert applied == []


def test_scan_gate_applies_matching_exception() -> None:
    exc = {"id": "CVE-1", "package": "zlib", "reason": "not reachable", "expires": "2099-01-01"}
    counts, blocking, applied = release.scan_gate(
        {"matches": [_match("CVE-1", "Critical"), _match("CVE-1", "Critical")]}, [exc]
    )
    assert blocking == [] and applied == [exc]
    md = release.scan_markdown(counts, blocking, applied)
    assert "not reachable" in md and "No unexcepted critical" in md


def test_scan_gate_exception_must_match_package() -> None:
    exc = {"id": "CVE-1", "package": "other", "reason": "r", "expires": "2099-01-01"}
    _, blocking, _ = release.scan_gate({"matches": [_match("CVE-1", "Critical")]}, [exc])
    assert len(blocking) == 1


def test_load_exceptions_rejects_expired_and_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "exc.json"
    today = dt.date(2026, 9, 24)
    path.write_text(
        json.dumps(
            {"exceptions": [{"id": "C", "package": "p", "reason": "r", "expires": "2026-09-23"}]}
        )
    )
    with pytest.raises(release.ReleaseError, match="expired"):
        release.load_exceptions(path, today)
    path.write_text(json.dumps({"exceptions": [{"id": "C", "package": "p"}]}))
    with pytest.raises(release.ReleaseError, match="missing"):
        release.load_exceptions(path, today)
    assert release.load_exceptions(tmp_path / "absent.json", today) == []


def test_repo_exceptions_file_is_valid() -> None:
    release.load_exceptions(ROOT / "deploy" / "vulnerability-exceptions.json", dt.date.today())


# --- release notes -------------------------------------------------------------


def test_release_notes_contain_the_release_contract() -> None:
    notes = release.release_notes(
        version="0.1.0",
        commit="0123abc",
        created="2026-09-24T00:00:00Z",
        image_repo=REPO,
        digest=DIGEST,
        changes="- First.",
        scan_summary="No unexcepted critical vulnerabilities.",
        tags=["v0.1.0", "0.1", "0", "latest"],
        provenance_url="https://example/prov",
        sbom_url="https://example/sbom",
    )
    assert f"{REPO}@{DIGEST}" in notes
    for needle in ("0123abc", "2026-09-24T00:00:00Z", "linux/amd64", "AVX2", "## Upgrade"):
        assert needle in notes
    assert "## Rollback" in notes and "No GPU acceleration is claimed" in notes


def test_release_notes_reject_bad_digest() -> None:
    with pytest.raises(release.ReleaseError):
        release.release_notes(
            version="0.1.0",
            commit="c",
            created="t",
            image_repo=REPO,
            digest="latest",
            changes="",
            scan_summary="",
            tags=["v0.1.0"],
            provenance_url="",
            sbom_url="",
        )


# --- CLI -----------------------------------------------------------------------


def test_cli_check_version_and_failure_exit(capsys: pytest.CaptureFixture[str]) -> None:
    assert release._cmd(["check-version"]) == 0
    assert capsys.readouterr().out.strip() == engine.__version__
    assert release._cmd(["check-version", "--tag", "v9.9.9"]) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_scan_gate_and_notes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = tmp_path / "grype.json"
    report.write_text(json.dumps({"matches": [_match("CVE-9", "Critical")]}))
    summary = tmp_path / "scan.md"
    exc = tmp_path / "exc.json"
    exc.write_text(json.dumps({"exceptions": []}))
    args = ["scan-gate", "--report", str(report), "--exceptions", str(exc)]
    assert release._cmd([*args, "--summary-out", str(summary)]) == 1
    assert "CVE-9" in summary.read_text(encoding="utf-8")

    report.write_text(json.dumps({"matches": []}))
    assert release._cmd([*args, "--summary-out", str(summary)]) == 0
    notes = tmp_path / "notes.md"
    assert (
        release._cmd(
            [
                "notes",
                "--version",
                engine.__version__,
                "--commit",
                "abc",
                "--created",
                "2026-09-24T00:00:00Z",
                "--image-repo",
                REPO,
                "--digest",
                DIGEST,
                "--scan-summary",
                str(summary),
                "--tags",
                f"v{engine.__version__}",
                "--out",
                str(notes),
            ]
        )
        == 0
    )
    assert f"{REPO}@{DIGEST}" in notes.read_text(encoding="utf-8")

    out = tmp_path / "compose.yaml"
    assert release._cmd(["render-compose", "--image", f"{REPO}@{DIGEST}", "--out", str(out)]) == 0
    assert DIGEST in out.read_text(encoding="utf-8")
    assert release._cmd(["stable-tags", "--version", "0.2.0", "v0.1.0"]) == 0
    assert "latest" in capsys.readouterr().out


# --- candidate identity --------------------------------------------------------


def _add(tf: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def _oci_archive(tmp_path: Path, *, arch: str = "amd64", count: int = 1) -> tuple[Path, str, str]:
    config = json.dumps({"os": "linux", "architecture": arch}).encode()
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    manifest = json.dumps({"config": {"digest": config_digest}, "layers": []}).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    desc = {"mediaType": release.OCI_MANIFEST, "digest": manifest_digest}
    path = tmp_path / "candidate.oci.tar"
    with tarfile.open(path, "w") as tf:
        _add(tf, "index.json", json.dumps({"manifests": [desc] * count}).encode())
        _add(tf, f"blobs/sha256/{manifest_digest[7:]}", manifest)
        _add(tf, f"blobs/sha256/{config_digest[7:]}", config)
    return path, manifest_digest, config_digest


def test_oci_digests_reads_manifest_and_config(tmp_path: Path) -> None:
    path, manifest, config = _oci_archive(tmp_path)
    assert release.oci_digests(path) == {
        "manifest": manifest,
        "config": config,
        "platform": "linux/amd64",
    }


def test_oci_digests_rejects_wrong_platform_and_multi_image(tmp_path: Path) -> None:
    with pytest.raises(release.ReleaseError, match="linux/amd64"):
        release.oci_digests(_oci_archive(tmp_path, arch="arm64")[0])
    with pytest.raises(release.ReleaseError, match="exactly one"):
        release.oci_digests(_oci_archive(tmp_path, count=2)[0])


@pytest.mark.parametrize("config_path", ["blobs/sha256/{h}", "{h}.json"])
def test_saved_config_digest_both_image_stores(config_path: str) -> None:
    h = "b" * 64
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        _add(tf, "manifest.json", json.dumps([{"Config": config_path.format(h=h)}]).encode())
    buf.seek(0)
    assert release.saved_config_digest(buf) == f"sha256:{h}"


def test_saved_config_digest_requires_manifest() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        _add(tf, "other", b"x")
    buf.seek(0)
    with pytest.raises(release.ReleaseError):
        release.saved_config_digest(buf)


# --- locked wheel-build tooling -------------------------------------------------

DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
LOCKED_INSTALL = (
    "pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/release-build.txt"
)


def _stages(dockerfile: str) -> dict[str, str]:
    parts = re.split(r"^FROM \S+ AS (\w+)\s*$", dockerfile, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_dockerfile_builds_the_wheel_with_locked_tools_only() -> None:
    build = _stages(DOCKERFILE)["build"]
    assert "COPY requirements/release-build.txt /tmp/release-build.txt" in build
    assert LOCKED_INSTALL in build
    # PEP 517 isolation would resolve pyproject's floating setuptools from PyPI.
    assert "python -m build --no-isolation --wheel" in build
    assert "--upgrade pip" not in DOCKERFILE
    # Every pip install reads a hash-locked file or installs the just-built wheel.
    for cmd in re.findall(r"pip install[^\n&]*", DOCKERFILE):
        assert "--require-hashes" in cmd or "--no-deps /tmp/*.whl" in cmd, cmd


def test_build_tools_stay_out_of_the_runtime_image() -> None:
    assert "release-build" not in _stages(DOCKERFILE)["runtime"]


def _locked_requirements(path: Path) -> dict[str, list[str]]:
    """``name -> hashes`` of a pip ``--require-hashes`` file; asserts its syntax."""
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("--index-url") or line.startswith("--extra-index-url"):
            continue
        req, *opts = line.split()
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9.+!-]*)", req)
        assert m, f"not an exact pin: {req!r}"
        hashes = [o.removeprefix("--hash=") for o in opts]
        assert all(o.startswith("--hash=sha256:") for o in opts), f"bad option in {line!r}"
        assert hashes and all(re.fullmatch(r"sha256:[0-9a-f]{64}", h) for h in hashes), req
        out[m[1].lower().replace("_", "-")] = hashes
    return out


def test_build_lock_pins_every_build_tool_with_hashes() -> None:
    locked = _locked_requirements(ROOT / "requirements" / "release-build.txt")
    assert {"pip", "build", "setuptools", "wheel", "packaging", "pyproject-hooks"} <= set(locked)
    # The wheel build backend itself must satisfy pyproject's build-system floor.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires = ["setuptools>=68", "wheel"]' in pyproject
    text = (ROOT / "requirements" / "release-build.txt").read_text(encoding="utf-8")
    setuptools = re.search(r"^setuptools==(\d+)\.", text, re.M)
    assert setuptools and int(setuptools[1]) >= 68


def test_runtime_lock_is_hash_locked_too() -> None:
    assert "llama-cpp-python" in _locked_requirements(ROOT / "requirements" / "release-cpu.txt")


@pytest.mark.parametrize(
    "bad",
    ["pip>=26 \\\n    --hash=sha256:" + "a" * 64, "pip==26.0", "pip==26.0 --hash=md5:abc"],
)
def test_lock_syntax_check_rejects_unpinned_or_unhashed(tmp_path: Path, bad: str) -> None:
    path = tmp_path / "lock.txt"
    path.write_text(bad + "\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        _locked_requirements(path)


# --- release-reachable workflows ------------------------------------------------

WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE_WORKFLOWS = ("release.yml", "ci.yml")
USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*['\"]?([^\s'\"#]+)", re.M)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def unpinned_actions(workflow_text: str) -> list[str]:
    """Third-party ``uses:`` refs that are not a full commit SHA (local ``./`` actions
    and reusable workflows in this repo are exempt)."""
    bad = []
    for ref in USES_RE.findall(workflow_text):
        if ref.startswith("./"):
            continue
        _, _, version = ref.partition("@")
        if not SHA_RE.match(version):
            bad.append(ref)
    return bad


def _workflow(name: str) -> dict[Any, Any]:
    return dict(yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8")))


def _triggers(name: str) -> dict[str, Any]:
    wf = _workflow(name)
    return dict(wf["on"] if "on" in wf else wf[True])  # YAML 1.1 reads bare `on:` as True


def _steps(job: str) -> list[dict[str, Any]]:
    return list(_workflow("release.yml")["jobs"][job]["steps"])


def _step_index(steps: list[dict[str, Any]], predicate: Any) -> int:
    return next(i for i, st in enumerate(steps) if predicate(st))


@pytest.mark.parametrize("name", RELEASE_WORKFLOWS)
def test_release_reachable_workflows_pin_actions_by_sha(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert USES_RE.findall(text), f"{name}: no uses: found — scanner broken?"
    assert unpinned_actions(text) == []
    # Every pin carries a human-readable version comment.
    for line in text.splitlines():
        if re.search(r"uses:\s*[^.\s]", line):
            assert re.search(r"@[0-9a-f]{40} # v\d", line), line
    _workflow(name)  # still valid YAML


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: actions/checkout@v4",
        "        uses: actions/cache@v4.3.0 # v4.3.0",
        "        uses: 'docker/setup-buildx-action@main'",
        "      - uses: actions/checkout@11d5960a",
        "      - uses: actions/checkout@" + "A" * 40,
        "      - uses: actions/checkout",
    ],
)
def test_unpinned_action_scanner_flags_moving_refs(line: str) -> None:
    assert unpinned_actions(f"steps:\n{line}\n")


def test_unpinned_action_scanner_accepts_sha_and_local() -> None:
    text = (
        "      - uses: actions/checkout@" + "0" * 40 + " # v4.4.0\n"
        "    uses: ./.github/workflows/ci.yml\n"
        "      - uses: ./.github/actions/setup\n"
    )
    assert unpinned_actions(text) == []


RELEASE_INPUT_PATHS = {
    "Dockerfile",
    ".dockerignore",
    "engine/**",
    "dashboard/**",
    "requirements/**",
    "pyproject.toml",
    "CHANGELOG.md",
    "deploy/**",
    "scripts/release.py",
    "scripts/release_image_e2e.py",
    ".github/workflows/release.yml",
    ".github/workflows/ci.yml",
}


def test_release_dry_run_covers_every_image_input() -> None:
    on = _triggers("release.yml")
    assert RELEASE_INPUT_PATHS <= set(on["pull_request"]["paths"])


def test_release_workflow_never_runs_on_branch_pushes() -> None:
    on = _triggers("release.yml")
    assert set(on) == {"push", "workflow_dispatch", "pull_request"}
    assert on["push"] == {"tags": ["v*.*.*"]}  # tags only; no branches
    assert on["workflow_dispatch"] is None  # no inputs => cannot request publish


def test_only_a_release_tag_on_main_sets_publish() -> None:
    script = _steps("validate")[1]["run"]
    tag_branch, other_branch = script.split("else", 1)
    assert 'if [ "$GITHUB_EVENT_NAME" = "push" ]' in tag_branch
    assert "check-version --tag" in tag_branch and "origin/main" in tag_branch
    assert "PUBLISH=true" in tag_branch
    assert "PUBLISH=false" in other_branch.split("fi", 1)[0]
    assert "PUBLISH=true" not in other_branch


def test_sbom_attestation_uses_supported_pinned_action() -> None:
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "actions/attest-sbom@" not in text
    step = next(st for st in _steps("publish") if st.get("id") == "sbom")
    action, _, sha = step["uses"].partition("@")
    assert action == "actions/attest" and SHA_RE.match(sha)
    assert step["with"] == {
        "subject-name": "${{ env.IMAGE_REPO }}",  # untagged repository
        "subject-digest": "${{ env.DIGEST }}",  # the tested candidate digest
        "sbom-path": "sbom.spdx.json",
        "push-to-registry": True,
        "create-storage-record": False,
    }
    assembly = next(st for st in _steps("publish") if st.get("name") == "Assemble release assets")
    assert "steps.sbom.outputs.bundle-path" in assembly["run"]
    assert "steps.sbom.outputs.attestation-url" in assembly["run"]


def test_image_e2e_is_given_the_candidate_metadata() -> None:
    step = next(st for st in _steps("candidate") if "release_image_e2e.py" in st.get("run", ""))
    for flag in (
        '--expect-version "$VERSION"',
        '--expect-commit "$COMMIT"',
        '--expect-built-at "$CREATED"',
    ):
        assert flag in step["run"]


def test_publish_verifies_downloaded_archive_before_push_or_attest() -> None:
    steps = _steps("publish")
    download = _step_index(steps, lambda st: "download-artifact" in st.get("uses", ""))
    verify = _step_index(steps, lambda st: "sha256sum -c" in st.get("run", ""))
    assert "oci-digests candidate.oci.tar" in steps[verify]["run"]
    assert '= "$DIGEST"' in steps[verify]["run"]
    pushes = [i for i, st in enumerate(steps) if "skopeo copy" in st.get("run", "")]
    attests = [i for i, st in enumerate(steps) if "attest" in st.get("uses", "")]
    assert pushes and attests
    assert download < verify < min(pushes + attests)


def test_stable_tags_only_after_both_attestations() -> None:
    steps = _steps("publish")
    provenance = _step_index(steps, lambda st: st.get("id") == "provenance")
    sbom = _step_index(steps, lambda st: st.get("id") == "sbom")
    tags = _step_index(steps, lambda st: st.get("id") == "tags")
    assert "stable-tags" in steps[tags]["run"]
    assert max(provenance, sbom) < tags
    # The only earlier registry write is the traceability sha-<commit> tag.
    earlier = [st["run"] for st in steps[:tags] if "skopeo copy" in st.get("run", "")]
    assert len(earlier) == 1 and '$IMAGE_REPO:$SHA_TAG"' in earlier[0]


# --- generated release artifacts ------------------------------------------------

IMMUTABLE_REF_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}")


def test_rendered_compose_has_exactly_one_immutable_image(tmp_path: Path) -> None:
    ref = f"{REPO}@sha256:{'0123456789abcdef' * 4}"
    out = tmp_path / "compose.yaml"
    assert release._cmd(["render-compose", "--image", ref, "--out", str(out)]) == 0
    rendered = out.read_text(encoding="utf-8")
    images = re.findall(r"^\s*image:\s*(\S+)\s*$", rendered, re.M)
    assert images == [ref]
    assert IMMUTABLE_REF_RE.findall(rendered) == [ref]
    service = yaml.safe_load(rendered)["services"]["inference-engine"]
    assert service["image"] == ref


def test_release_notes_carry_the_same_image_and_digest(tmp_path: Path) -> None:
    digest = "sha256:" + "0123456789abcdef" * 4
    ref = f"{REPO}@{digest}"
    compose = release.render_compose(
        (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8"), ref
    )
    notes = release.release_notes(
        version="0.1.0",
        commit="c" * 40,
        created="2026-09-24T00:00:00Z",
        image_repo=REPO,
        digest=digest,
        changes="- x",
        scan_summary="ok",
        tags=["v0.1.0"],
        provenance_url="p",
        sbom_url="s",
    )
    assert set(IMMUTABLE_REF_RE.findall(notes)) == set(IMMUTABLE_REF_RE.findall(compose)) == {ref}
    assert f"| Digest | `{digest}` |" in notes


@pytest.mark.parametrize(
    "ref", [f"{REPO}:v0.1.0", f"{REPO}:latest", f"{REPO}:sha-0123456789ab", f"{REPO}@sha256:AB"]
)
def test_cli_refuses_to_render_a_mutable_compose(tmp_path: Path, ref: str) -> None:
    out = tmp_path / "compose.yaml"
    assert release._cmd(["render-compose", "--image", ref, "--out", str(out)]) == 1
    assert not out.exists()


# --- image E2E: /version contract -----------------------------------------------

E2E_BASE_ARGS = ["--image", "img", "--expect-image-id", "sha256:x", "--model", "m.gguf"]
E2E_BASE_ARGS += ["--model-sha256", "h"]
GOOD_META = {
    "--expect-version": "0.1.0",
    "--expect-commit": "0123456789abcdef0123456789abcdef01234567",
    "--expect-built-at": "2026-09-24T12:34:56Z",
}


def _e2e_argv(**override: str) -> list[str]:
    meta = {**GOOD_META, **{f"--{k.replace('_', '-')}": v for k, v in override.items()}}
    return [*E2E_BASE_ARGS, *(x for kv in meta.items() for x in kv)]


def test_e2e_accepts_candidate_metadata() -> None:
    args = e2e.parse_args(_e2e_argv())
    assert (args.expect_version, args.expect_commit, args.expect_built_at) == tuple(
        GOOD_META.values()
    )


@pytest.mark.parametrize("missing", list(GOOD_META))
def test_e2e_requires_all_release_metadata(missing: str) -> None:
    argv = [*E2E_BASE_ARGS]
    for flag, value in GOOD_META.items():
        if flag != missing:
            argv += [flag, value]
    with pytest.raises(SystemExit):
        e2e.parse_args(argv)


@pytest.mark.parametrize(
    "override",
    [
        {"expect_version": "v0.1.0"},
        {"expect_version": "0.1"},
        {"expect_version": ""},
        {"expect_commit": "0123abc"},
        {"expect_commit": "0123456789ABCDEF0123456789ABCDEF01234567"},
        {"expect_built_at": "2026-09-24"},
        {"expect_built_at": "2026-09-24T12:34:56+00:00"},
        {"expect_built_at": "2026-13-24T12:34:56Z"},
        {"expect_built_at": ""},
    ],
)
def test_e2e_rejects_malformed_release_metadata(override: dict[str, str]) -> None:
    with pytest.raises(SystemExit):
        e2e.parse_args(_e2e_argv(**override))


EXPECTED_VERSION = {
    "version": "0.1.0",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "built_at": "2026-09-24T12:34:56Z",
}


def test_version_response_matching_passes() -> None:
    assert e2e.version_mismatches(dict(EXPECTED_VERSION), EXPECTED_VERSION) == []
    assert e2e.version_mismatches({**EXPECTED_VERSION, "extra": 1}, EXPECTED_VERSION) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "0.1.1"),
        ("commit", "0123456"),
        ("commit", None),
        ("built_at", "2026-09-24T12:34:57Z"),
        ("built_at", None),
    ],
)
def test_version_response_mismatch_fails(field: str, value: Any) -> None:
    wrong = e2e.version_mismatches({**EXPECTED_VERSION, field: value}, EXPECTED_VERSION)
    assert len(wrong) == 1 and wrong[0].startswith(f"{field}=")


@pytest.mark.parametrize("body", [None, [], "0.1.0", {}])
def test_version_response_missing_fields_fails(body: Any) -> None:
    assert e2e.version_mismatches(body, EXPECTED_VERSION)
