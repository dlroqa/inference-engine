"""Tests for scripts/release.py — the fail-closed release gates."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import engine

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_tool", ROOT / "scripts" / "release.py")
assert _spec and _spec.loader
release = importlib.util.module_from_spec(_spec)
sys.modules["release_tool"] = release
_spec.loader.exec_module(release)

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
