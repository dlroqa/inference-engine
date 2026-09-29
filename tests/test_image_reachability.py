"""Unit tests for the triage reachability probe and its read-only audit workflow."""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
import types
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "image_reachability", ROOT / "scripts" / "image_reachability.py"
)
assert _spec and _spec.loader
probe = importlib.util.module_from_spec(_spec)
sys.modules["image_reachability"] = probe
_spec.loader.exec_module(probe)

WORKFLOW = ROOT / ".github" / "workflows" / "image-audit.yml"


def test_imported_modules_sees_plain_and_from_imports() -> None:
    tree = ast.parse(
        "import zipfile\nfrom urllib.request import HTTPPasswordMgr\n"
        "from . import tarfile\nimport http.cookies as c\n"
    )
    found = probe.imported_modules(tree)
    assert {"zipfile", "urllib.request", "http.cookies"} <= found
    assert "tarfile" not in found  # relative import of a local module, not the stdlib


def test_module_match_is_by_package_boundary() -> None:
    assert probe.matches("http.cookies", "http.cookies")
    assert probe.matches("encodings.idna", "encodings")
    assert not probe.matches("zipfile36", "zipfile")


def test_recorder_counts_calls_once_a_target_module_loads() -> None:
    recorder = probe.Recorder()
    module = types.ModuleType("base64")
    module.b64decode = lambda data: data  # type: ignore[attr-defined]
    recorder.patch(module)
    module.b64decode(b"x")
    module.b64decode(b"y")
    assert recorder.calls == {"base64.b64decode": 2}


def test_recorder_keeps_only_listed_audit_events() -> None:
    recorder = probe.Recorder()
    recorder.audit("open", ("/etc/passwd", "r", 0))
    recorder.audit("ctypes.dlopen", ("libz.so.1",))
    recorder.audit("subprocess.Popen", ("ls", ["ls"], None, None))
    assert set(recorder.events) == {"ctypes.dlopen", "subprocess.Popen"}
    assert recorder.details["ctypes.dlopen"] == {"libz.so.1"}


def test_audit_workflow_is_read_only_pinned_and_never_publishes() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    on = workflow.get("on", workflow.get(True))
    assert set(on) == {"workflow_dispatch", "pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in workflow["jobs"].values())
    for ref in re.findall(r"uses:\s*([^\s#]+)", text):
        assert re.search(r"@[0-9a-f]{40}$", ref), ref
    for forbidden in ("docker push", "docker login", "skopeo", "gh release", "packages: write"):
        assert forbidden not in text
