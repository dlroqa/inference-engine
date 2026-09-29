"""Unit tests for the triage reachability probe and its read-only audit workflow."""

from __future__ import annotations

import ast
import http.server
import importlib.util
import json
import re
import subprocess
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest
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
    # Where the calls came from (this test), not the probe's own frames.
    origins = recorder.origins["base64.b64decode"]
    assert len(origins) == 2  # two call sites
    assert all(o.split(" <- ")[0].endswith("_once_a_target_module_loads") for o in origins)


def test_recorder_notes_who_imported_a_target_module() -> None:
    recorder = probe.Recorder()
    recorder.loaded(types.ModuleType("stringprep"))
    assert "test_recorder_notes_who_imported" in next(iter(recorder.origins["import stringprep"]))


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


# --- ELF inspection must never turn a failure into a negative finding ------------


def _elf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF" + b"\0" * 60)
    return path


def _readelf(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["readelf"], returncode, stdout, stderr)

    return fake_run


DYNSYM = """
Symbol table '.dynsym' contains 3 entries:
   Num:    Value          Size Type    Bind   Vis      Ndx Name
     1: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND gzwrite@ZLIB_1.2.0 (2)
     2: 0000000000001000    10 FUNC    GLOBAL DEFAULT   12 strfmon@@GLIBC_2.2.5
"""


def test_a_failed_readelf_raises_instead_of_reporting_no_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe.subprocess, "run", _readelf(1, stderr="readelf: Error: bad"))
    with pytest.raises(probe.InspectionError, match="exit 1"):
        probe.dynamic_symbols(_elf(tmp_path / "lib.so"))


def test_a_missing_readelf_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("readelf")

    monkeypatch.setattr(probe.subprocess, "run", missing)
    with pytest.raises(probe.InspectionError, match="did not run"):
        probe.dynamic_symbols(_elf(tmp_path / "lib.so"))


def test_dynamic_symbols_parses_imports_and_definitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe.subprocess, "run", _readelf(0, DYNSYM))
    syms = probe.dynamic_symbols(_elf(tmp_path / "lib.so"))
    assert syms.imported == {"gzwrite"} and syms.defined == {"strfmon"} and syms.has_dynsym


def test_elf_report_marks_failed_files_unassessed_and_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = _elf(tmp_path / "usr/lib/good.so")
    _elf(tmp_path / "usr/lib/broken.so")
    (tmp_path / "usr/lib/readme.txt").write_text("not elf")

    def fake_symbols(path: Path, timeout: float = 60) -> Any:
        if path == good:
            return probe.DynSyms({"gzwrite"}, set(), True)
        raise probe.InspectionError("readelf exit 1: Error")

    monkeypatch.setattr(probe, "dynamic_symbols", fake_symbols)
    report = probe.elf_report(tmp_path)
    done = report["completeness"]
    assert done["status"] == "incomplete"
    assert (done["regular_files_examined"], done["elf_discovered"], done["elf_inspected"]) == (
        3,
        2,
        1,
    )
    assert done["unassessed"] == [{"path": "/usr/lib/broken.so", "reason": "readelf exit 1: Error"}]
    gz = report["groups"]["zlib: CVE-2026-85091 gz write path"]["gzwrite"]
    assert gz["imported_by"] == ["/usr/lib/good.so"]
    text = probe.elf_markdown(report)
    assert "**incomplete**" in text and "/usr/lib/broken.so" in text
    assert "**none**" not in text  # no blanket negative claim
    assert "no direct dynamic-symbol import in the 1 inspected files" in text


def test_elf_report_lists_unreadable_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = _elf(tmp_path / "usr/lib/secret.so")
    real_open = Path.open

    def guarded_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == secret:
            raise PermissionError(13, "Permission denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    report = probe.elf_report(tmp_path)
    assert report["completeness"]["status"] == "incomplete"
    assert report["completeness"]["unassessed"] == [
        {"path": "/usr/lib/secret.so", "reason": "PermissionError: Permission denied"}
    ]


def test_elf_report_flags_files_without_a_dynamic_symbol_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _elf(tmp_path / "sbin/static-tool")
    no_table = _readelf(0, "\nDynamic symbol info is not available\n")
    monkeypatch.setattr(probe.subprocess, "run", no_table)
    done = probe.elf_report(tmp_path)["completeness"]
    assert done["status"] == "complete" and done["elf_without_dynsym"] == ["/sbin/static-tool"]


def test_elf_cli_exits_nonzero_on_an_incomplete_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _elf(tmp_path / "rootfs/lib.so")
    monkeypatch.setattr(probe.subprocess, "run", _readelf(2, stderr="boom"))
    out = tmp_path / "elf.json"
    assert probe.main(["elf", str(tmp_path / "rootfs"), "--json", str(out)]) == 3
    assert json.loads(out.read_text())["completeness"]["status"] == "incomplete"
    allowed = ["elf", str(tmp_path / "rootfs"), "--json", str(out), "--allow-incomplete"]
    assert probe.main(allowed) == 0


def test_static_markdown_makes_unparsed_files_visible() -> None:
    report = {
        "completeness": {
            "status": "incomplete",
            "python_files_parsed": 10,
            "unparsed": ["pkg:pkg/bad.py: SyntaxError"],
            "distributions_without_record": [],
            "native_extension_files_not_scanned": {"pkg": 2},
        },
        "imports_by_distribution": {"imaplib": {}},
        "sqlite": {
            "sqlite_version": "3",
            "session_compiled_in": True,
            "load_extension_available": True,
            "compile_options": [],
        },
        "zlib": {},
    }
    text = probe.static_markdown(report)
    assert "**incomplete**" in text and "pkg/bad.py" in text
    assert "no import/reference in the 10 parsed files" in text


# --- workload: outcomes are checked, not just printed ---------------------------


class _Engine(http.server.BaseHTTPRequestHandler):
    behaviour: dict[str, Any] = {}

    def log_message(self, *_args: Any) -> None:
        pass

    def _send(self, status: int, body: Any, content_type: str = "application/json") -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        failing = self.behaviour.get("fail_get")
        if self.path == failing:
            self._send(500, {"error": "x"})
        elif self.path == "/readyz" and self.behaviour.get("never_ready"):
            self._send(503, {})
        else:
            self._send(200, b"<html></html>" if self.path == "/dashboard/" else {})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/admin/models/import":
            self._send(200, {"id": "m1", "status": "ready", "sha256": "abc"})
        elif self.path.endswith("/load"):
            self._send(200, {})
        elif self.path == "/v1/chat/completions" and body.get("stream"):
            chunks = self.behaviour.get(
                "stream",
                [
                    {"choices": [{"index": 0, "delta": {"content": "one"}}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                    "[DONE]",
                ],
            )
            lines = [f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks]
            self._send(200, "".join(lines).encode(), "text/event-stream")
        elif self.path == "/v1/chat/completions":
            text = self.behaviour.get("chat_text", "hello")
            choice = {"message": {"content": text}, "finish_reason": "stop"}
            self._send(200, {"choices": [choice]})
        elif self.path == "/v1/messages":
            blocks = [{"type": "text", "text": "hi"}]
            self._send(200, {"content": blocks, "stop_reason": "end_turn"})
        else:
            self._send(404, {})


@pytest.fixture
def engine() -> Any:
    _Engine.behaviour = {}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _run(server: Any, **behaviour: Any) -> tuple[bool, Any]:
    _Engine.behaviour = behaviour
    workload = probe.Workload(f"http://127.0.0.1:{server.server_address[1]}", "k", timeout=5)
    return workload.run("/data/m.gguf", "audit", ready_within=2), workload


def _failed(result: tuple[bool, Any]) -> list[str]:
    ok, workload = result
    failed = [s["step"] for s in workload.steps if not s["ok"]]
    assert ok == (not failed)  # the overall result agrees with the steps
    return failed


def test_workload_passes_when_every_outcome_is_as_expected(engine: Any) -> None:
    result = _run(engine)
    assert _failed(result) == []
    workload = result[1]
    stream = next(s for s in workload.steps if s["step"] == "OpenAI streamed completion")
    assert stream == {
        "step": "OpenAI streamed completion",
        "ok": True,
        "chunks": 2,
        "content_chars": 3,
        "finish_reason": "stop",
    }
    assert "hello" not in json.dumps(workload.steps)  # no generated text is kept


def test_workload_fails_on_an_endpoint_error(engine: Any) -> None:
    assert _failed(_run(engine, fail_get="/admin/system")) == ["GET /admin/system"]


def test_workload_fails_a_stream_without_done(engine: Any) -> None:
    unfinished = [
        {"choices": [{"index": 0, "delta": {"content": "one"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    assert _failed(_run(engine, stream=unfinished)) == ["OpenAI streamed completion"]


def test_workload_fails_an_empty_completion(engine: Any) -> None:
    assert _failed(_run(engine, chat_text="")) == ["OpenAI chat completion"]


def test_workload_fails_when_the_model_never_becomes_ready(engine: Any) -> None:
    assert "load model + ready" in _failed(_run(engine, never_ready=True))


def test_workload_cli_reads_the_key_from_the_environment_only(
    engine: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Engine.behaviour = {"fail_get": "/metrics"}
    monkeypatch.setenv("IE_AUDIT_KEY", "secret-key")
    out = tmp_path / "workload.json"
    base = f"http://127.0.0.1:{engine.server_address[1]}"
    argv = ["workload", "--base", base, "--model-path", "/m", "--json", str(out)]
    assert probe.main([*argv, "--ready-within", "2", "--timeout", "5"]) == 1
    summary = out.read_text()
    assert json.loads(summary)["ok"] is False and "secret-key" not in summary
