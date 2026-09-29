"""Bounded reachability probes for the candidate image (triage follow-ups F3/F4a).

Evidence for docs/security/candidate-image-triage.md, collected in GitHub
Actions by .github/workflows/image-audit.yml. Stdlib only. Nothing here changes
the image or a release gate. Every result is an observation with a stated scope,
not a proof that a function is never reached.

Subcommands:
    elf ROOTFS          Runner side, on the image's exported filesystem: which
                        shared objects import or define the affected zlib, glibc
                        and SQLite functions (``readelf --dyn-syms``), embedded
                        zlib copies, ``,ccs=`` fopen mode strings, JISX0213 gconv
                        modules and nscd.
    python-static       Inside the image: which installed distributions import
                        the affected stdlib modules (AST), plus the sqlite3 and
                        zlib runtime identity.
    workload            Runner side: drive the recorded serve session (import,
                        load, OpenAI plain + streamed, Anthropic, admin reads)
                        and fail unless every step has the expected outcome.
    record -- ARGS      Inside the image: run ``inference-engine ARGS`` with an
                        audit hook and post-import counters, then write what the
                        process loaded and called to ``--out`` on exit.

``elf`` and ``python-static`` report what they could not inspect and exit 3 when
the inspection is incomplete, so an unreadable file or a failed ``readelf`` is
never presented as a negative finding.
"""

from __future__ import annotations

import argparse
import ast
import atexit
import collections
import importlib.abc
import importlib.machinery
import json
import os
import re
import subprocess
import sys
import sysconfig
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

# --- elf ---------------------------------------------------------------------

# Advisory -> the exported entry points that reach the affected code.
ELF_TARGETS: dict[str, dict[str, list[str]]] = {
    "zlib": {
        # CVE-2026-85091: gz_vacate() via non-blocking gzwrite/gzprintf.
        "CVE-2026-85091 gz write path": [
            "gzwrite",
            "gzfwrite",
            "gzprintf",
            "gzvprintf",
            "gzputc",
            "gzputs",
            "gzflush",
            "gzsetparams",
            "gzclose_w",
        ],
        "gzFile open (any gz* use)": ["gzopen", "gzopen64", "gzdopen", "gzopen_w"],
        # CVE-2026-27171: x2nmodp loop via the crc32 combine functions.
        "CVE-2026-27171 crc32 combine": [
            "crc32_combine",
            "crc32_combine64",
            "crc32_combine_gen",
            "crc32_combine_gen64",
            "crc32_combine_op",
        ],
    },
    "glibc": {
        "CVE-2026-19499 strfmon": ["strfmon", "strfmon_l"],
        "CVE-2026-5435/6238 ns_printrr/fp_nquery": [
            "ns_printrr",
            "ns_printrrf",
            "ns_sprintrr",
            "ns_sprintrrf",
            "fp_nquery",
            "fp_query",
            "p_query",
        ],
        "CVE-2026-19542 tdelete": ["tdelete"],
        "CVE-2026-6791/6368 wordexp": ["wordexp", "wordfree"],
        "CVE-2026-77117/80489 iconv (JISX0213)": ["iconv_open", "iconv"],
        "CVE-2010-4756 glob": ["glob", "glob64"],
        "CVE-2018-20796/2019-9192 regexec": ["regexec", "regcomp"],
    },
    "sqlite": {
        "CVE-2026-50812/50813 session extension": [
            "sqlite3session_create",
            "sqlite3changeset_apply",
            "sqlite3changeset_apply_v2",
            "sqlite3changeset_apply_v3",
            "sqlite3changeset_concat",
            "sqlite3changegroup_new",
            "sqlite3changegroup_add",
        ],
        "extension loading (zipfile/session at runtime)": [
            "sqlite3_load_extension",
            "sqlite3_enable_load_extension",
        ],
    },
}
EMBEDDED_ZLIB = re.compile(rb"(?:deflate|inflate) (\d+\.\d+(?:\.\d+)*) Copyright")
CCS_MODE = re.compile(rb"[rwa]\+?[a-z]*,ccs=")
GCONV_JISX0213 = ("EUC-JISX0213.so", "SHIFT_JISX0213.so")


class InspectionError(Exception):
    """An ELF file could not be inspected; its absence of findings means nothing."""


def _rel(root: Path, path: str | os.PathLike[str]) -> str:
    try:
        return "/" + str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def _reason(exc: OSError) -> str:
    return f"{type(exc).__name__}: {exc.strerror or exc}"


def elf_candidates(root: Path) -> tuple[list[Path], int, list[dict[str, str]]]:
    """(ELF files, regular files examined, files/directories that could not be read)."""
    elves: list[Path] = []
    unreadable: list[dict[str, str]] = []
    examined = 0

    def walk_error(exc: OSError) -> None:
        unreadable.append({"path": _rel(root, exc.filename or ""), "reason": _reason(exc)})

    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error):
        if Path(dirpath) == root:
            dirnames[:] = [d for d in dirnames if d not in ("proc", "sys", "dev")]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            examined += 1
            try:
                with path.open("rb") as handle:
                    if handle.read(4) == b"\x7fELF":
                        elves.append(path)
            except OSError as exc:
                unreadable.append({"path": _rel(root, path), "reason": _reason(exc)})
    return elves, examined, unreadable


class DynSyms:
    def __init__(self, imported: set[str], defined: set[str], has_dynsym: bool) -> None:
        self.imported = imported
        self.defined = defined
        self.has_dynsym = has_dynsym


def dynamic_symbols(path: Path, timeout: float = 60) -> DynSyms:
    """Imported and defined dynamic symbols (no version suffixes).

    Raises InspectionError when readelf fails, so a failure is never read as
    "no symbols". A file without ``.dynsym`` (for example a static binary) is
    reported as such: it can hold code that no import table shows.
    """
    try:
        proc = subprocess.run(
            ["readelf", "-W", "--dyn-syms", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionError(f"readelf did not run: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        detail = " ".join(proc.stderr.split())[-300:]
        raise InspectionError(f"readelf exit {proc.returncode}: {detail}")
    imported: set[str] = set()
    defined: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        # Num: Value Size Type Bind Vis Ndx Name
        if len(parts) < 8 or not parts[0].endswith(":") or parts[3] not in ("FUNC", "NOTYPE"):
            continue
        name = parts[7].split("@")[0]
        (imported if parts[6] == "UND" else defined).add(name)
    return DynSyms(imported, defined, "Symbol table '.dynsym'" in proc.stdout)


def elf_report(root: Path) -> dict[str, Any]:
    wanted = {sym for groups in ELF_TARGETS.values() for syms in groups.values() for sym in syms}
    importers: dict[str, list[str]] = collections.defaultdict(list)
    definers: dict[str, list[str]] = collections.defaultdict(list)
    embedded: list[dict[str, Any]] = []
    ccs: list[str] = []
    elves, examined, unassessed = elf_candidates(root)
    inspected = 0
    no_dynsym: list[str] = []
    for path in elves:
        rel = _rel(root, path)
        try:
            syms = dynamic_symbols(path)
            data = path.read_bytes()
        except InspectionError as exc:
            unassessed.append({"path": rel, "reason": str(exc)})
            continue
        except OSError as exc:
            unassessed.append({"path": rel, "reason": _reason(exc)})
            continue
        inspected += 1
        if not syms.has_dynsym:
            no_dynsym.append(rel)
        for sym in syms.imported & wanted:
            importers[sym].append(rel)
        for sym in syms.defined & wanted:
            definers[sym].append(rel)
        if not Path(rel).name.startswith("libz.so"):
            versions = sorted({m.decode() for m in EMBEDDED_ZLIB.findall(data)})
            if versions:
                embedded.append({"path": rel, "zlib_versions": versions})
        if not Path(rel).name.startswith(("libc.so", "libc-")) and CCS_MODE.search(data):
            ccs.append(rel)
    gconv = sorted(str(p.relative_to(root)) for p in root.glob("usr/lib/*/gconv/*JISX0213*"))
    report: dict[str, Any] = {
        "completeness": {
            "status": "complete" if not unassessed else "incomplete",
            "regular_files_examined": examined,
            "elf_discovered": len(elves),
            "elf_inspected": inspected,
            "elf_without_dynsym": sorted(no_dynsym),
            "unassessed": sorted(unassessed, key=lambda u: u["path"]),
        },
        "groups": {},
    }
    for lib, groups in ELF_TARGETS.items():
        for label, target_syms in groups.items():
            report["groups"][f"{lib}: {label}"] = {
                sym: {"defined_in": sorted(definers[sym]), "imported_by": sorted(importers[sym])}
                for sym in target_syms
            }
    report["embedded_zlib_version_strings"] = embedded
    report["ccs_fopen_mode_strings_outside_libc"] = sorted(ccs)
    report["jisx0213_gconv_modules"] = gconv
    report["nscd_present"] = [
        p for p in ("usr/sbin/nscd", "var/run/nscd/socket") if (root / p).exists()
    ]
    return report


def elf_markdown(report: dict[str, Any]) -> str:
    done = report["completeness"]
    lines = [
        f"Completeness: **{done['status']}**. Regular files examined "
        f"{done['regular_files_examined']}; ELF discovered {done['elf_discovered']}, "
        f"inspected {done['elf_inspected']}, unassessed {len(done['unassessed'])}; "
        f"inspected without a dynamic symbol table {len(done['elf_without_dynsym'])}.",
        "",
    ]
    lines += [f"- unassessed `{u['path']}`: {u['reason']}" for u in done["unassessed"]]
    lines += [f"- no `.dynsym` (imports not visible): `{p}`" for p in done["elf_without_dynsym"]]
    none = f"no direct dynamic-symbol import in the {done['elf_inspected']} inspected files"
    lines += ["", "| Group | Symbol | Defined in | Imported by |", "| --- | --- | --- | --- |"]
    for group, syms in report["groups"].items():
        for sym, where in syms.items():
            defined = ", ".join(where["defined_in"]) or "-"
            used = ", ".join(where["imported_by"]) or none
            lines.append(f"| {group} | `{sym}` | {defined} | {used} |")
    lines.append("")
    embedded = report["embedded_zlib_version_strings"]
    lines.append(f"zlib version strings outside libz (string search): {embedded or 'none found'}")
    ccs = report["ccs_fopen_mode_strings_outside_libc"]
    lines.append(f"`,ccs=` mode strings outside libc (string search): {ccs or 'none found'}")
    lines.append(f"JISX0213 gconv modules: {report['jisx0213_gconv_modules'] or 'none'}")
    lines.append(f"nscd present: {report['nscd_present'] or 'no'}")
    return "\n".join(lines)


# --- python-static -------------------------------------------------------------

STDLIB_TARGETS = (
    "imaplib",
    "poplib",
    "http.cookies",
    "zipfile",
    "stringprep",
    "encodings.idna",
    "tarfile",
    "base64",
    "urllib.request",
    "sqlite3",
    "diskcache",
)
NAME_TARGETS = ("HTTPPasswordMgr", "LlamaDiskCache", "set_cache", "load_extension")


def imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def matches(module: str, target: str) -> bool:
    return module == target or module.startswith(target + ".")


def python_static() -> dict[str, Any]:
    import importlib.metadata as metadata
    import sqlite3
    import zlib

    targets = (*STDLIB_TARGETS, *NAME_TARGETS)
    per_target: dict[str, dict[str, list[str]]] = {t: {} for t in targets}
    unparsed: list[str] = []
    no_record: list[str] = []
    native: collections.Counter[str] = collections.Counter()
    parsed = 0
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if dist.files is None:
            no_record.append(name)  # no RECORD: its files cannot be enumerated
            continue
        for file in dist.files:
            if file.suffix == ".so":
                native[name] += 1  # compiled code: not visible to an AST scan
            if file.suffix != ".py":
                continue
            path = Path(str(dist.locate_file(file)))
            try:
                tree = ast.parse(path.read_bytes())  # honours PEP 263 declarations
            except (OSError, SyntaxError, ValueError) as exc:
                unparsed.append(f"{name}:{file}: {type(exc).__name__}")
                continue
            parsed += 1
            modules = imported_modules(tree)
            for target in STDLIB_TARGETS:
                if any(matches(m, target) for m in modules):
                    per_target[target].setdefault(name, []).append(str(file))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
                n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            }
            for target in NAME_TARGETS:
                if target in names:
                    per_target[target].setdefault(name, []).append(str(file))
    conn = sqlite3.connect(":memory:")
    options = [row[0] for row in conn.execute("PRAGMA compile_options")]
    conn.close()
    lib_zlib: str | None = None
    try:
        import ctypes

        libz = ctypes.CDLL("libz.so.1")
        libz.zlibVersion.restype = ctypes.c_char_p
        lib_zlib = libz.zlibVersion().decode()
    except OSError as exc:
        lib_zlib = f"unavailable: {exc}"
    return {
        "python": sys.version,
        "completeness": {
            "status": "complete" if not (unparsed or no_record) else "incomplete",
            "python_files_parsed": parsed,
            "unparsed": unparsed,
            "distributions_without_record": no_record,
            "native_extension_files_not_scanned": dict(sorted(native.items())),
        },
        "imports_by_distribution": per_target,
        "sqlite": {
            "sqlite_version": sqlite3.sqlite_version,
            "compile_options": options,
            "session_compiled_in": any("SESSION" in o for o in options),
            "connection_api": sorted(a for a in dir(sqlite3.Connection) if not a.startswith("_")),
            "load_extension_available": hasattr(sqlite3.Connection, "enable_load_extension"),
        },
        "zlib": {
            "module_compiled_against": zlib.ZLIB_VERSION,
            "module_runtime": zlib.ZLIB_RUNTIME_VERSION,
            "libz_zlibVersion": lib_zlib,
        },
    }


def static_markdown(report: dict[str, Any]) -> str:
    done = report["completeness"]
    lines = [
        f"Completeness: **{done['status']}**. Python files parsed "
        f"{done['python_files_parsed']}; unparsed {len(done['unparsed'])}; distributions "
        f"without RECORD {len(done['distributions_without_record'])}; native extension files "
        f"(not scanned) {sum(done['native_extension_files_not_scanned'].values())}.",
        "",
    ]
    lines += [f"- unparsed: {u}" for u in done["unparsed"]]
    lines += [f"- no RECORD: {d}" for d in done["distributions_without_record"]]
    none = f"no import/reference in the {done['python_files_parsed']} parsed files"
    lines += ["", "| Module / name | Distributions (files) |", "| --- | --- |"]
    for target, dists in report["imports_by_distribution"].items():
        cell = "; ".join(f"{d} ({len(f)}): {', '.join(f[:4])}" for d, f in sorted(dists.items()))
        lines.append(f"| `{target}` | {cell or none} |")
    sqlite = report["sqlite"]
    lines += [
        "",
        f"SQLite {sqlite['sqlite_version']}; "
        f"session compiled in: {sqlite['session_compiled_in']}; "
        f"enable_load_extension present: {sqlite['load_extension_available']}",
        f"compile options: {', '.join(sqlite['compile_options'])}",
        f"zlib: {report['zlib']}",
    ]
    return "\n".join(lines)


# --- record ----------------------------------------------------------------------

AUDIT_EVENTS = (
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "ctypes.dlopen",
    "socket.getaddrinfo",
    "sqlite3.connect",
    "sqlite3.enable_load_extension",
    "sqlite3.load_extension",
    "shutil.unpack_archive",
    "urllib.Request",
)
# module -> attributes whose calls are counted once the module is imported.
CALL_TARGETS: dict[str, tuple[str, ...]] = {
    "tarfile": ("open", "TarFile.extractall", "TarFile.extract", "TarFile.addfile"),
    "zipfile": ("ZipFile.__init__", "ZipFile.open"),
    "imaplib": ("IMAP4.__init__",),
    "poplib": ("POP3.__init__",),
    "http.cookies": ("BaseCookie.load", "Morsel.js_output"),
    "urllib.request": ("HTTPPasswordMgr.__init__", "HTTPBasicAuthHandler.__init__"),
    "base64": (
        "b64decode",
        "standard_b64decode",
        "urlsafe_b64decode",
        "b32decode",
        "b16decode",
        "a85decode",
    ),
    "pkgutil": ("get_data",),
    "gzip": ("GzipFile.__init__",),
    "diskcache": ("Cache.__init__", "FanoutCache.__init__"),
    "llama_cpp.llama_cache": ("LlamaDiskCache.__init__", "LlamaRAMCache.__init__"),
    "llama_cpp.llama": ("Llama.set_cache",),
    "stringprep": ("in_table_a1", "in_table_b2"),
    "encodings.idna": ("ToASCII",),
}
PATH_PREFIXES = (
    (sysconfig.get_paths()["purelib"], ""),
    (sysconfig.get_paths()["platlib"], ""),
    (sysconfig.get_paths()["stdlib"], "stdlib/"),
)


def short_path(filename: str) -> str:
    for prefix, tag in PATH_PREFIXES:
        if filename.startswith(prefix + os.sep):
            return tag + filename[len(prefix) + 1 :]
    return filename


def origin(depth: int = 6) -> str:
    """The calling frames (innermost first), without this probe's and frozen frames."""
    frames: list[str] = []
    frame = sys._getframe(1)
    while frame is not None and len(frames) < depth:
        name = frame.f_code.co_filename
        if name != __file__ and not name.startswith("<frozen"):
            frames.append(f"{short_path(name)}:{frame.f_lineno} {frame.f_code.co_name}")
        frame = frame.f_back
    return " <- ".join(frames)


class Recorder:
    def __init__(self) -> None:
        self.events: collections.Counter[str] = collections.Counter()
        self.details: dict[str, set[str]] = collections.defaultdict(set)
        self.calls: collections.Counter[str] = collections.Counter()
        self.origins: dict[str, set[str]] = collections.defaultdict(set)

    def note(self, label: str) -> None:
        if len(self.origins[label]) < 5:
            self.origins[label].add(origin())

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if not event.startswith(AUDIT_EVENTS):
            return
        self.events[event] += 1
        if event in ("ctypes.dlopen", "socket.getaddrinfo", "sqlite3.connect"):
            self.details[event].add(str(args[0])[:200])
        elif event.startswith(("subprocess", "os.")):
            self.details[event].add(repr(args[:2])[:200])
        if not event.startswith("sqlite3.connect"):
            self.note(event)

    def wrap(self, module: ModuleType, dotted: str) -> None:
        owner: Any = module
        *parents, attr = dotted.split(".")
        for part in parents:
            owner = getattr(owner, part, None)
            if owner is None:
                return
        original = getattr(owner, attr, None)
        if not callable(original):
            return
        label = f"{module.__name__}.{dotted}"
        counter = self.calls
        note = self.note

        def counted(*args: Any, **kwargs: Any) -> Any:
            counter[label] += 1
            note(label)
            return original(*args, **kwargs)

        if isinstance(owner, type) and isinstance(owner.__dict__.get(attr), staticmethod):
            setattr(owner, attr, staticmethod(counted))
        elif isinstance(owner, type) and isinstance(owner.__dict__.get(attr), classmethod):
            return  # not needed for the targets above
        else:
            setattr(owner, attr, counted)

    def patch(self, module: ModuleType) -> None:
        for dotted in CALL_TARGETS.get(module.__name__, ()):
            self.wrap(module, dotted)

    def loaded(self, module: ModuleType) -> None:
        self.note(f"import {module.__name__}")
        self.patch(module)


class PostImportPatcher(importlib.abc.MetaPathFinder):
    """Wraps the loader of each CALL_TARGETS module so it is patched once loaded."""

    def __init__(self, on_loaded: Callable[[ModuleType], None]) -> None:
        self.on_loaded = on_loaded

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname not in CALL_TARGETS:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is None or spec.loader is None:
                continue
            loader: Any = spec.loader

            def exec_module(
                module: ModuleType,
                _orig: Any = loader.exec_module,
                _done: Callable[[ModuleType], None] = self.on_loaded,
            ) -> None:
                _orig(module)
                _done(module)

            try:
                loader.exec_module = exec_module
            except (AttributeError, TypeError):
                pass
            return spec
        return None


def record(argv: list[str], out: Path) -> int:
    recorder = Recorder()
    before = set(sys.modules)
    # Modules already loaded by this probe itself are patched now and flagged.
    for name in CALL_TARGETS:
        if name in sys.modules:
            recorder.patch(sys.modules[name])
    sys.meta_path.insert(0, PostImportPatcher(recorder.loaded))
    sys.addaudithook(recorder.audit)

    def dump() -> None:
        loaded = sorted(
            t
            for t in (*STDLIB_TARGETS, "gzip", "pkgutil", "llama_cpp.llama_cache")
            if t in sys.modules
        )
        out.write_text(
            json.dumps(
                {
                    "argv": argv,
                    "loaded_before_engine": sorted(t for t in CALL_TARGETS if t in before),
                    "target_modules_loaded": loaded,
                    "calls": dict(sorted(recorder.calls.items())),
                    "audit_events": dict(sorted(recorder.events.items())),
                    "audit_details": {k: sorted(v) for k, v in recorder.details.items()},
                    "origins": {k: sorted(v) for k, v in sorted(recorder.origins.items())},
                },
                indent=2,
            )
        )

    atexit.register(dump)
    from engine.cli import main as engine_main

    return engine_main(argv)


# --- workload ----------------------------------------------------------------------

GET_ENDPOINTS = (
    "/admin/overview",
    "/admin/system",
    "/admin/models",
    "/admin/keys",
    "/metrics",
    "/logs",
    "/dashboard/",
    "/version",
    "/readyz",
)


class WorkloadFailure(Exception):
    pass


class Workload:
    """Drives the recorded serve session and checks each outcome.

    The summary keeps only step names, HTTP statuses, counts and pass/fail. It
    never keeps prompts, generated text, response bodies or the key.
    """

    def __init__(self, base: str, key: str, timeout: float = 120.0) -> None:
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.steps: list[dict[str, Any]] = []

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, auth: bool = True
    ) -> Any:
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", f"Bearer {self.key}")
        if path == "/v1/messages":
            req.add_header("anthropic-version", "2023-06-01")
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            return exc
        except (urllib.error.URLError, OSError) as exc:
            raise WorkloadFailure(f"{method} {path}: {type(exc).__name__}") from exc

    def step(self, name: str, check: Callable[[], dict[str, Any]]) -> None:
        try:
            detail = check()
            self.steps.append({"step": name, "ok": True, **detail})
        except WorkloadFailure as exc:
            self.steps.append({"step": name, "ok": False, "reason": str(exc)})
        except Exception as exc:  # malformed responses count as failures, not crashes
            self.steps.append({"step": name, "ok": False, "reason": type(exc).__name__})

    def json_ok(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        with self.request(method, path, body) as resp:
            if resp.status != 200:
                raise WorkloadFailure(f"{method} {path}: HTTP {resp.status}")
            return json.loads(resp.read())

    def wait_status(self, path: str, want: int, within: float, auth: bool = False) -> None:
        import time

        deadline = time.monotonic() + within
        last = "no response"
        while time.monotonic() < deadline:
            try:
                with self.request("GET", path, auth=auth) as resp:
                    if resp.status == want:
                        return
                    last = f"HTTP {resp.status}"
            except WorkloadFailure as exc:
                last = str(exc)
            time.sleep(0.5)
        raise WorkloadFailure(f"GET {path} not {want} within {within:.0f}s ({last})")

    def stream(self, model: str) -> dict[str, Any]:
        body = {
            "model": model,
            "stream": True,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Count to three."}],
        }
        chunks = content = 0
        finish: str | None = None
        done = False
        with self.request("POST", "/v1/chat/completions", body) as resp:
            if resp.status != 200:
                raise WorkloadFailure(f"stream: HTTP {resp.status}")
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    done = True
                    break
                if finish is not None:
                    raise WorkloadFailure("stream: chunk after the finishing chunk")
                choice = json.loads(payload)["choices"][0]
                chunks += 1
                content += len(choice.get("delta", {}).get("content") or "")
                finish = choice.get("finish_reason") or finish
        if not (content and finish in ("stop", "length") and done):
            raise WorkloadFailure(
                f"stream incomplete: content_chars={content} finish={finish} done={done}"
            )
        return {"chunks": chunks, "content_chars": content, "finish_reason": finish}

    def run(self, model_path: str, model: str, ready_within: float = 120.0) -> bool:
        self.step("healthz", lambda: self.wait_status("/healthz", 200, ready_within) or {})
        model_id: dict[str, str] = {}

        def import_model() -> dict[str, Any]:
            got = self.json_ok("POST", "/admin/models/import", {"path": model_path, "name": model})
            if got.get("status") != "ready" or not got.get("id"):
                raise WorkloadFailure(f"import: status={got.get('status')}")
            model_id["id"] = got["id"]
            return {"status": 200, "model_status": "ready", "sha256": got.get("sha256")}

        self.step("import model", import_model)

        def load() -> dict[str, Any]:
            if "id" not in model_id:
                raise WorkloadFailure("no imported model")
            self.json_ok("POST", f"/admin/models/{model_id['id']}/load")
            self.wait_status("/readyz", 200, ready_within)
            return {"status": 200, "readyz": 200}

        self.step("load model + ready", load)

        def chat() -> dict[str, Any]:
            got = self.json_ok(
                "POST",
                "/v1/chat/completions",
                {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
            )
            choice = got["choices"][0]
            text = choice["message"]["content"]
            finish = choice["finish_reason"]
            if not (isinstance(text, str) and text) or finish not in ("stop", "length"):
                raise WorkloadFailure(f"chat: finish={finish}")
            return {"status": 200, "content_chars": len(text), "finish_reason": finish}

        self.step("OpenAI chat completion", chat)
        self.step("OpenAI streamed completion", lambda: self.stream(model))

        def messages() -> dict[str, Any]:
            got = self.json_ok(
                "POST",
                "/v1/messages",
                {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
            )
            blocks = [b for b in got["content"] if b.get("type") == "text" and b.get("text")]
            if not blocks or got.get("stop_reason") not in ("end_turn", "max_tokens"):
                raise WorkloadFailure(f"messages: stop_reason={got.get('stop_reason')}")
            chars = sum(len(b["text"]) for b in blocks)
            return {"status": 200, "content_chars": chars, "stop_reason": got["stop_reason"]}

        self.step("Anthropic messages", messages)
        for path in GET_ENDPOINTS:

            def get(path: str = path) -> dict[str, Any]:
                with self.request("GET", path) as resp:
                    if resp.status != 200:
                        raise WorkloadFailure(f"GET {path}: HTTP {resp.status}")
                    resp.read()
                return {"status": 200}

            self.step(f"GET {path}", get)
        return all(step["ok"] for step in self.steps)


# --- cli --------------------------------------------------------------------------


INCOMPLETE_EXIT = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    elf = sub.add_parser("elf")
    elf.add_argument("rootfs", type=Path)
    static = sub.add_parser("python-static")
    for command in (elf, static):
        command.add_argument("--json", type=Path, required=True)
        command.add_argument(
            "--allow-incomplete",
            action="store_true",
            help=f"Report an incomplete inspection with exit 0 instead of {INCOMPLETE_EXIT}.",
        )
    work = sub.add_parser("workload", help="Drive and check the recorded serve session.")
    work.add_argument("--base", default="http://127.0.0.1:8000")
    work.add_argument("--model-path", required=True, help="GGUF path inside the container.")
    work.add_argument("--model", default="audit")
    work.add_argument("--json", type=Path, required=True)
    work.add_argument("--ready-within", type=float, default=120.0)
    work.add_argument("--timeout", type=float, default=120.0)
    rec = sub.add_parser("record")
    rec.add_argument("--out", type=Path, required=True)
    rec.add_argument("engine_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.cmd in ("elf", "python-static"):
        report = elf_report(args.rootfs) if args.cmd == "elf" else python_static()
        args.json.write_text(json.dumps(report, indent=2))
        print(elf_markdown(report) if args.cmd == "elf" else static_markdown(report))
        complete = report["completeness"]["status"] == "complete"
        return 0 if complete or args.allow_incomplete else INCOMPLETE_EXIT
    if args.cmd == "workload":
        key = os.environ.get("IE_AUDIT_KEY", "")
        if not key:
            parser.error("set IE_AUDIT_KEY (the key is read from the environment only)")
        workload = Workload(args.base, key, timeout=args.timeout)
        ok = workload.run(args.model_path, args.model, ready_within=args.ready_within)
        args.json.write_text(json.dumps({"ok": ok, "steps": workload.steps}, indent=2))
        for step in workload.steps:
            print(f"[{'PASS' if step['ok'] else 'FAIL'}] {step['step']}", flush=True)
        return 0 if ok else 1
    engine_args = [a for a in args.engine_args if a != "--"]
    return record(engine_args, args.out)


if __name__ == "__main__":
    sys.exit(main())
