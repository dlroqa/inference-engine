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
    record -- ARGS      Inside the image: run ``inference-engine ARGS`` with an
                        audit hook and post-import counters, then write what the
                        process loaded and called to ``--out`` on exit.
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
from collections.abc import Callable, Iterator, Sequence
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


def elf_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath) == root:
            dirnames[:] = [d for d in dirnames if d not in ("proc", "sys", "dev")]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    if handle.read(4) == b"\x7fELF":
                        yield path
            except OSError:
                continue


def dynamic_symbols(path: Path) -> tuple[set[str], set[str]]:
    """(imported, defined) dynamic symbol names, without version suffixes."""
    out = subprocess.run(
        ["readelf", "-W", "--dyn-syms", str(path)], capture_output=True, text=True
    ).stdout
    imported: set[str] = set()
    defined: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        # Num: Value Size Type Bind Vis Ndx Name
        if len(parts) < 8 or not parts[0].endswith(":") or parts[3] not in ("FUNC", "NOTYPE"):
            continue
        name = parts[7].split("@")[0]
        (imported if parts[6] == "UND" else defined).add(name)
    return imported, defined


def elf_report(root: Path) -> dict[str, Any]:
    wanted = {sym for groups in ELF_TARGETS.values() for syms in groups.values() for sym in syms}
    importers: dict[str, list[str]] = collections.defaultdict(list)
    definers: dict[str, list[str]] = collections.defaultdict(list)
    embedded: list[dict[str, Any]] = []
    ccs: list[str] = []
    count = 0
    for path in elf_files(root):
        count += 1
        rel = "/" + str(path.relative_to(root))
        imported, defined = dynamic_symbols(path)
        for sym in imported & wanted:
            importers[sym].append(rel)
        for sym in defined & wanted:
            definers[sym].append(rel)
        data = path.read_bytes()
        if not Path(rel).name.startswith("libz.so"):
            versions = sorted({m.decode() for m in EMBEDDED_ZLIB.findall(data)})
            if versions:
                embedded.append({"path": rel, "zlib_versions": versions})
        if not Path(rel).name.startswith(("libc.so", "libc-")) and CCS_MODE.search(data):
            ccs.append(rel)
    gconv = sorted(str(p.relative_to(root)) for p in root.glob("usr/lib/*/gconv/*JISX0213*"))
    report: dict[str, Any] = {"elf_files_scanned": count, "groups": {}}
    for lib, groups in ELF_TARGETS.items():
        for label, syms in groups.items():
            report["groups"][f"{lib}: {label}"] = {
                sym: {"defined_in": sorted(definers[sym]), "imported_by": sorted(importers[sym])}
                for sym in syms
            }
    report["embedded_zlib_copies"] = embedded
    report["ccs_fopen_mode_strings_outside_libc"] = sorted(ccs)
    report["jisx0213_gconv_modules"] = gconv
    report["nscd_present"] = [
        p for p in ("usr/sbin/nscd", "var/run/nscd/socket") if (root / p).exists()
    ]
    return report


def elf_markdown(report: dict[str, Any]) -> str:
    lines = [f"ELF files scanned: {report['elf_files_scanned']}", ""]
    lines += ["| Group | Symbol | Defined in | Imported by |", "| --- | --- | --- | --- |"]
    for group, syms in report["groups"].items():
        for sym, where in syms.items():
            defined = ", ".join(where["defined_in"]) or "-"
            used = ", ".join(where["imported_by"]) or "**none**"
            lines.append(f"| {group} | `{sym}` | {defined} | {used} |")
    lines.append("")
    lines.append(f"Embedded zlib copies: {report['embedded_zlib_copies'] or 'none'}")
    ccs = report["ccs_fopen_mode_strings_outside_libc"]
    lines.append(f"`,ccs=` mode strings outside libc: {ccs or 'none'}")
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
    errors: list[str] = []
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        for file in dist.files or []:
            if file.suffix != ".py":
                continue
            path = Path(str(dist.locate_file(file)))
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
            except (OSError, SyntaxError, ValueError) as exc:
                errors.append(f"{name}:{file}: {type(exc).__name__}")
                continue
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
        "imports_by_distribution": per_target,
        "parse_errors": errors,
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
    lines = ["| Module / name | Distributions (files) |", "| --- | --- |"]
    for target, dists in report["imports_by_distribution"].items():
        cell = "; ".join(f"{d} ({len(f)}): {', '.join(f[:4])}" for d, f in sorted(dists.items()))
        lines.append(f"| `{target}` | {cell or '**none**'} |")
    sqlite = report["sqlite"]
    lines += [
        "",
        f"SQLite {sqlite['sqlite_version']}; "
        f"session compiled in: {sqlite['session_compiled_in']}; "
        f"enable_load_extension present: {sqlite['load_extension_available']}",
        f"compile options: {', '.join(sqlite['compile_options'])}",
        f"zlib: {report['zlib']}",
        f"parse errors: {len(report['parse_errors'])}",
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
    "zipfile": ("ZipFile.__init__",),
    "imaplib": ("IMAP4.__init__",),
    "poplib": ("POP3.__init__",),
    "http.cookies": ("BaseCookie.load",),
    "urllib.request": ("HTTPPasswordMgr.__init__", "HTTPBasicAuthHandler.__init__"),
    "base64": ("b64decode", "urlsafe_b64decode", "b32decode", "b16decode", "a85decode"),
    "gzip": ("GzipFile.__init__",),
    "diskcache": ("Cache.__init__", "FanoutCache.__init__"),
    "llama_cpp.llama_cache": ("LlamaDiskCache.__init__", "LlamaRAMCache.__init__"),
    "llama_cpp.llama": ("Llama.set_cache",),
    "stringprep": ("in_table_a1",),
    "encodings.idna": ("ToASCII",),
}


class Recorder:
    def __init__(self) -> None:
        self.events: collections.Counter[str] = collections.Counter()
        self.details: dict[str, set[str]] = collections.defaultdict(set)
        self.calls: collections.Counter[str] = collections.Counter()

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if not event.startswith(AUDIT_EVENTS):
            return
        self.events[event] += 1
        if event in ("ctypes.dlopen", "socket.getaddrinfo", "sqlite3.connect"):
            self.details[event].add(str(args[0])[:200])
        elif event.startswith(("subprocess", "os.")):
            self.details[event].add(repr(args[:2])[:200])

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

        def counted(*args: Any, **kwargs: Any) -> Any:
            counter[label] += 1
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
    sys.meta_path.insert(0, PostImportPatcher(recorder.patch))
    sys.addaudithook(recorder.audit)

    def dump() -> None:
        loaded = sorted(
            t for t in (*STDLIB_TARGETS, "gzip", "llama_cpp.llama_cache") if t in sys.modules
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
                },
                indent=2,
            )
        )

    atexit.register(dump)
    from engine.cli import main as engine_main

    return engine_main(argv)


# --- cli --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    elf = sub.add_parser("elf")
    elf.add_argument("rootfs", type=Path)
    elf.add_argument("--json", type=Path, required=True)
    static = sub.add_parser("python-static")
    static.add_argument("--json", type=Path, required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--out", type=Path, required=True)
    rec.add_argument("engine_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.cmd == "elf":
        report = elf_report(args.rootfs)
        args.json.write_text(json.dumps(report, indent=2))
        print(elf_markdown(report))
        return 0
    if args.cmd == "python-static":
        report = python_static()
        args.json.write_text(json.dumps(report, indent=2))
        print(static_markdown(report))
        return 0
    engine_args = [a for a in args.engine_args if a != "--"]
    return record(engine_args, args.out)


if __name__ == "__main__":
    sys.exit(main())
