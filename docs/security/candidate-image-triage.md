# Candidate image vulnerability triage (initial)

Status: **initial triage, 2026-09-27; revised 2026-09-28 after review;
reachability evidence added and corrected 2026-09-29** (see §3a, §3b, §4a, §6).
This is not a vulnerability-free claim and not production-release approval.

**Current state (2026-09-29):** no urgent blocker has been identified in the
assessed findings; unresolved investigations may change that decision (§4a).
Current dispositions are in §3a, and which of them the user has accepted is in
§3b (none yet). The §3 table and §4 are the dated 2026-09-28 record, and §3a
supersedes them where they differ. Follow-ups (§5): F1 is **integrated** (#45,
merged as `199c4da`; not deployed); F6 is **integrated** (#46, merged as
`84b6817`; main CI 36612186495 green; retention after a failing Critical gate
not yet exercised); F3/F4a are partially investigated; F2, F4b and F5 are open.

The release gate is unchanged. It fails only on unexcepted **Critical** findings
(`docs/releasing.md`, "Vulnerability gate"). No exceptions were added, no
findings were suppressed, and no dependencies were changed for this report.

## 1. Evidence

### Fresh scan (the evidence for this report)

| Field | Value |
| --- | --- |
| Source content | `main` @ `0379f3c` (merge of #38). Tree `cd14671` is identical to the synthetic merge `b69f65d` that Release 36305266676 built |
| Scan checkout | `c607c56` on the temporary diagnostic branch `ci/candidate-scan` = `0379f3c` + one diagnostic workflow file. `.github/` does not reach the runtime stage; only the build-arg commit label differs |
| Workflow | [Candidate scan (diagnostic) 36317166186, attempt 1](https://github.com/dlroqa/inference-engine/actions/runs/36317166186). Same `docker buildx` invocation and the same pinned `anchore/sbom-action` v0.24.2 and `anchore/scan-action` v7.4.2 as `release.yml`. Non-publishing, read-only token |
| Image | linux/amd64, manifest `sha256:5f30f47344cd952640060082a014781b87cbb45a4a4472c6c6f3ebe8e0bf7d36`, config `sha256:5f84e0fdda4fae03ef0d3e5f77ddec40aad8faf6873070431da703fbd6dcf8e8` (a rebuild: the digest differs from any published or earlier candidate) |
| Base | `python:3.12-slim@sha256:2f17fc04…`, Debian GNU/Linux 13 (trixie), CPython 3.12.14 |
| Scanner | grype 0.118.0; DB schema v6.1.9, built 2026-09-27T06:30:30Z; providers include debian, github, nvd, kev, epss |
| Scan time | 2026-09-27T11:55:16Z |
| Artifact | `candidate-scan` (30-day retention): `sbom.spdx.json`, `grype.json`, `dpkg.tsv`, `pip-list.json`, `dists.json`, `os-release.txt`, `history.txt`, `scan-summary.md` |

### Historical baseline (labelled, not re-used as fresh evidence)

Release [36305266676](https://github.com/dlroqa/inference-engine/actions/runs/36305266676)
(`e0683a9`, synthetic merge `b69f65d`) and Release 36286879928 (`382fd2e`)
reported the same counts. `release.yml` keeps the detailed SBOM and grype report
only when it publishes, so those runs left no per-finding detail. That is why a
fresh scan was needed (see follow-up F6).

| Severity | Historical (36305266676) | Fresh (36317166186) | Unique advisories (fresh) |
| --- | ---: | ---: | ---: |
| Critical | 0 | 0 | 0 |
| High | 50 | 50 | 13 |
| Medium | 64 | 64 | 39 |
| Low | 10 | 10 | 6 |
| Negligible | 45 | 45 | 24 |
| Unknown | 0 | 0 | 0 |
| **Total** | 169 | 169 | 82 |

Counts are scanner matches. One advisory matched against, for example, nine
util-linux binary packages from one source package counts nine times.

### What is in the runtime image

The runtime stage is the pinned `python:3.12-slim` base plus the hash-locked
`requirements/release-cpu.txt` set and the engine wheel. The Node dashboard
stage and the wheel-build stage are discarded, so no build-only tools are
scanned, apart from `pip`, which the base image ships (see G2). The container
runs as uid 10001 (`engine`) and executes `inference-engine serve` plus a
`python -c urllib` health check. ~~The engine does not use `subprocess`
(verified by source search)~~ **Corrected 2026-09-29:** the engine's source
never imports `subprocess`, but a recorded serve session (§3a) shows one
indirect spawn: `platform.processor()` in `engine/telemetry/hardware.py` runs
`uname -p` (fixed argv, no shell, no user input). No other process was observed in
the recorded serve, backup and restore sessions. The remaining Debian command-line tools are
present but were not invoked.

## 2. Exploitation evidence

- **KEV:** no advisory in this report matched grype's `kev` provider feed
  (captured 2026-09-27T00:37Z). This claim is scoped to that feed; it was not
  independently checked against the CISA catalogue.
- **EPSS:** the highest advisory is 0.0576 (CVE-2018-20796, Negligible, glibc).
  Every High is below 0.0065.
- Neither fact is treated as proof of safety. They only affect priority.

## 3. Findings by group (2026-09-28 record; see §3a for current dispositions)

"Reachability" separates **verified** facts (source search of `engine/`, image
inventories, upstream or Debian tracker text) from **assumptions**, which are
labelled. Absence of a direct call from the engine's Python code is recorded as
such; it does **not** establish that CPython itself or a native dependency never
reaches the affected function. Those paths stay unassessed (F4a). Owners and
review points are in §5; they are **proposed**, not accepted assignments, and
nothing here records the user accepting residual risk.

| Group | Advisories (H/M/L/N) | Matches | Fix status (verified source) | Runtime relevance | Disposition |
| --- | --- | ---: | --- | --- | --- |
| **G1** CPython 3.12.14 interpreter | 1/9/1/1 | 12 | 3.12.14 (2026-08-12) is the newest 3.12 release ([python.org](https://www.python.org/downloads/)). Grype lists fixes only on 3.13/3.14/3.15 lines. CVE-2026-82049: upstream fix [cpython#157192](https://github.com/python/cpython/pull/157192), [announcement](https://mail.python.org/archives/list/security-announce@python.org/thread/EFJWGAZJA56AKSBR2WHMHQZO7RRLZPRH/) gives no 3.12 release | **Present and used** (runs the engine). Per module, verified: `tarfile` (82049 High, 19672, 87910) is used only by the operator-run `inference-engine restore` CLI on an operator-supplied archive. `_safe_members` admits just three named regular files (`isfile()` rejects hard links, symlinks and directories), then `filter="data"`. Not reachable from the network. `base64` (12781, 3446) decodes only the engine's own webhook secret. `urllib.request` is used with the default opener, so `HTTPPasswordMgr` (15806) is not used. `imaplib`/`poplib`/`http.cookies`/`zipfile` are not used by `engine/`. **Unassessed:** use of these modules by third-party dependencies; `stringprep`/IDNA (17084) for non-ASCII hostnames in operator-supplied download or webhook URLs | **Schedule update** (F5): when a newer 3.12 release or refreshed `python:3.12-slim` digest appears, verify advisory by advisory from its release notes which of these it fixes (no future release is assumed to fix all), then re-scan. **Investigate** (F3): dependency use of the affected stdlib modules. Not urgent |
| **G2** pip 25.0.1 | 0/5/1/0 | 6 | Fixed in pip 25.3–26.2.0 per GHSA records (see appendix) | **Present, not used at runtime** (verified: no engine, entrypoint or health-check use). pip ships with the base image. Exploitation needs someone running `pip install` inside the container | **Schedule update** (F2): remove pip from the runtime layer after `pip check`, or install a locked patched pip. Validate with the image E2E. Not urgent |
| **G3** diskcache 5.6.3 (GHSA-w8v5-vhqr-4h9v, pickle deserialization) | 0/1/0/0 | 1 | No fixed version (grype: not-fixed) | Locked dependency of `llama-cpp-python`. `engine/` never uses diskcache or llama cache APIs (verified). Exploitation needs write access to a cache directory. **Assumption:** llama-cpp-python does not create a disk cache unless asked | **Investigate** (F3, low): confirm there is no default disk-cache use; otherwise justified non-applicability |
| **G4** zlib1g | 1/1/0/0 | 2 | CVE-2026-85091 (High, `gz_vacate` heap overflow in non-blocking `gzwrite`): an **upstream fix exists** ([madler/zlib@df84af2](https://github.com/madler/zlib/commit/df84af25dc1942490e1d1c899a07619152a46148), [issue #1310](https://github.com/madler/zlib/issues/1310)), but **no fixed Debian package** exists: bookworm, trixie, forky and sid all vulnerable/unfixed, rechecked 2026-09-28 ([tracker](https://security-tracker.debian.org/tracker/CVE-2026-85091), bug #1146895). CVE-2026-27171 (CPU use in `crc32_combine64`): Debian wont-fix | Library present and used by CPython's `zlib`/`gzip` (backups use `tarfile` `w:gz`). **Assumption:** CPython's zlib module uses the deflate/inflate stream API, not the `gzFile` API where `gz_vacate` lives; native wheels (llama.cpp etc.) are unaudited for `gzwrite` use | **Investigate now** (F4a native/CPython reachability audit, which does not wait for a package) and **monitor** (F4b) for a fixed Debian package. No Debian base image fixes it today. Highest-priority open item; exposure neither demonstrated nor excluded |
| **G5** glibc (libc6, libc-bin) | 2/9/2/7 | 40 | CVE-2026-19499 (strfmon): Debian "minor issue, no DSA", fixed only in 2.43-5 (sid/forky) ([tracker](https://security-tracker.debian.org/tracker/CVE-2026-19499)). The others are wont-fix or not-fixed per grype's Debian data | Present in every process. The affected functions (`strfmon`, deprecated `ns_printrr`/`fp_nquery`, `fopen ,ccs=`, `tdelete`, `wordexp`/tilde, `iconv` JISX0213, `nscd`, `ld.so` TOCTOU) are not called by the engine's Python code. **Assumption:** CPython and native wheels do not call them on network input. The resolver search-list issue (8674) needs control of `resolv.conf`/`LOCALDOMAIN`, which the operator or runtime owns | **Provisional low priority** pending F4a (CPython/native reachability is unassessed); fixes arrive through base refreshes |
| **G6** util-linux family (bsdutils, libblkid1, libmount1, libsmartcols1, libuuid1, liblastlog2-2, login, mount, util-linux) | 4/1/0/1 | 54 | CVE-2026-76642: Debian trixie vulnerable, **no-dsa, minor issue**; fixed in forky 2.42.3-1 ([tracker](https://security-tracker.debian.org/tracker/CVE-2026-76642)). 78408/78409/78410/3184: wont-fix per grype | Tools not invoked by the engine. **Relevant as post-compromise escalation:** `mount` (and `su`) are normally setuid in Debian (**assumption**: not yet listed from this image, part of F1), and `deploy/compose.yaml` sets neither `no-new-privileges` nor `cap_drop`. Code execution as uid 10001 could attempt local escalation inside the container. Needs a prior compromise | **Schedule hardening** (F1): add `security_opt: [no-new-privileges:true]` and `cap_drop: [ALL]` to Compose (and the docs), validated by the image E2E. No urgent blocker has been identified in the assessed findings; unresolved investigations may change that decision. **2026-09-29:** integrated by #45 (merged as `199c4da`, §3a); deployments gain it only when their Compose file is updated |
| **G7** ncurses (libncursesw6, libtinfo6, ncurses-base, ncurses-bin) | 1/1/0/0 | 8 | wont-fix (grype/Debian) | `infocmp`/form library, not used by the engine | Justified non-applicability (no terminal UI in the product); re-check at base refresh |
| **G8** perl-base | 2/2/0/1 | 5 | CVE-2026-82560 (Pod::Text): trixie vulnerable, no-dsa, unfixed in sid ([tracker](https://security-tracker.debian.org/tracker/CVE-2026-82560)). CVE-2026-9538 (Archive::Tar) and others: wont-fix | Essential Debian package (dpkg scripts). The engine never runs perl | Justified non-applicability; re-check at base refresh |
| **G9** acl/attr (libacl1, libattr1) | 2/1/0/0 | 3 | wont-fix (grype/Debian) | Local symlink/TOCTOU issues in path-based ACL functions and `getfattr`/`setfattr`. Not used by the engine | Justified non-applicability for the product; covered by F1 hardening |
| **G10** Linux-PAM (4 packages) | 0/1/0/0 | 4 | wont-fix | `pam_userdb` is not configured; no login service in the container | Justified non-applicability |
| **G11** systemd libraries (libsystemd0, libudev1) | 0/2/1/4 | 14 | wont-fix/not-fixed | `systemd-oomd`/`homed`/`journald` are not running in the container | Justified non-applicability |
| **G12** SQLite (libsqlite3-0) | 0/2/0/2 | 4 | wont-fix/not-fixed | **Used**: the engine store is SQLite through Python `sqlite3`. The Medium issues are in the Session extension, which Python 3.12's `sqlite3` module does not expose (**assumption** from the module's documented API; not checked in the image). CVE-2021-45346 and CVE-2025-70873 are **not individually assessed** | **Provisional / investigate** (F3): confirm in Actions that the image's Python `sqlite3` exposes no Session API and whether `libsqlite3-0` is built with the Session extension; assess the two Negligibles. Not a justified non-applicability until then |
| **G13** GNU tar | 0/3/0/1 | 4 | wont-fix/not-fixed | `tar` binary not used (backups use Python `tarfile`) | Justified non-applicability |
| **G14** bzip2 (libbz2-1.0) | 0/1/0/0 | 1 | wont-fix | Bug is in the `bzip2recover` utility | Justified non-applicability |
| **G15** shadow (login.defs, passwd) | 0/0/1/1 | 4 | wont-fix/not-fixed | No interactive logins | Justified non-applicability |
| **G16** coreutils, diffutils, apt | 0/0/0/6 | 7 | not-fixed | Tools not invoked | Justified non-applicability |

"Justified non-applicability" means not reachable through the product as
built and run. It is not "resolved": the packages remain installed and
re-appear in every scan.

## 3a. Reachability evidence (F1, F3, F4a; 2026-09-29)

The §3 table is the dated 2026-09-28 record; this section supersedes its
dispositions where they differ. Nothing here changes a gate, exception,
dependency or finding. Proposed dispositions are **proposals**; §3b records which
ones the user has accepted (none so far). User acceptance does not strengthen
the technical evidence.

### Evidence identity

| Source | Run | What it is |
| --- | --- | --- |
| Image reachability audit (F3/F4a), draft PR #47 | [36523922216](https://github.com/dlroqa/inference-engine/actions/runs/36523922216) attempt 1: checkout `c6d9544` (synthetic merge of PR head `6f042d6` into `f121e91`) | `.github/workflows/image-audit.yml` + `scripts/image_reachability.py`, with completeness checks, the enforced workload and the source trust chain. **Raw evidence retained in `docs/security/evidence/image-audit-2026-09-29/`** (checksums verified) |
| Earlier audit runs (same PR) | [36514927554](https://github.com/dlroqa/inference-engine/actions/runs/36514927554), [36515304639](https://github.com/dlroqa/inference-engine/actions/runs/36515304639), [36515849536](https://github.com/dlroqa/inference-engine/actions/runs/36515849536) | Same ELF, static and zlib-source findings, but these runs **did not enforce** inspection completeness or workload outcomes. They recorded HTTP 200 and serve exit 0 without asserting them. They are kept as observations only |
| Release dry run of F1, PR #45 (merged 2026-09-29 as `199c4da`; main CI [36565420928](https://github.com/dlroqa/inference-engine/actions/runs/36565420928) green) | [36514494606](https://github.com/dlroqa/inference-engine/actions/runs/36514494606) | Privilege inventory + image E2E + ownership E2E under the hardened Compose settings |

The audit image is a **rebuild**: manifest `sha256:4c53eef1…`, config
`sha256:f3b3c78e…`, with the same Dockerfile, base digests and lock files as §1.
It is not byte-identical to the scanned image of §1 or to any published image.
Package versions match §1 (`dpkg.tsv`: `zlib1g 1:1.3.dfsg+really1.3.1-1+b1`,
SQLite 3.46.1, CPython 3.12.14).

### Method, completeness and limits

- **ELF:** `readelf --dyn-syms` on every ELF file in the exported root
  filesystem. Completeness **complete**: 7,843 regular files examined, 814 ELF
  discovered, 814 inspected, 0 unassessed. One ELF has no dynamic symbol table
  (`python.o`), so its references are not visible. A failed inspection would
  have been listed and would have failed the job (exit 3).
  This shows **direct dynamic-symbol imports only**. It does not see calls
  resolved at runtime (`dlsym`), calls inside one library, or stripped or
  statically linked copies. String searches (zlib version strings, `,ccs=`
  modes) found nothing, which is a bounded observation, not proof of absence.
- **Static:** AST scan of all 1,432 `.py` files of every installed distribution
  (complete: 0 unparsed, none without RECORD). Compiled extension modules
  (40 files, for example numpy, llama_cpp, websockets, uvloop, httptools) are
  **not** scanned.
- **Recorded:** `inference-engine serve`, `backup` and `restore` ran under an
  audit hook with post-import call counters and calling stacks. The serve
  session was driven and **checked** step by step: health; GGUF import (status
  ready, sha256); load and `/readyz` 200; OpenAI chat (non-empty content,
  `finish_reason`); OpenAI stream (content, finishing chunk, then `[DONE]`);
  Anthropic `/v1/messages` (text content, `stop_reason`); and 200 from
  `/admin/{overview,system,models,keys}`, `/metrics`, `/logs`, `/dashboard/`,
  `/version` and `/readyz`. Serve exit code 0 with a `drain_complete` event,
  backup archive created and restore succeeded were all asserted. All 15 steps
  passed. The summaries keep statuses and counts only.
  A recording shows what this session did. **Not exercised:** remote backends,
  webhook delivery, model downloads, WebSocket traffic, billing, other request
  shapes and settings.

### G4 zlib: CVE-2026-85091 (High)

- **Source evidence.** The advisory text gives zlib 1.3.1.2 through 1.3.2, and
  the non-blocking `gz*` support that contains `gz_vacate` first appeared
  upstream in 1.3.1.2. The audit fetched the image's Debian source through a
  checked chain:
  - The trust anchor is the Debian archive keyring in the digest-pinned base
    image.
  - trixie `InRelease` verified with three valid signatures (`gpgv-status.txt`).
  - `Sources.xz` checksum → zlib paragraph at version `1:1.3.dfsg+really1.3.1-1`
    → `.dsc`, orig and Debian tarball checksums.
  - After applying the Debian patch series (empty), `zlib.h` says `1.3.1` and
    `gzwrite.c` contains `gz_vacate` 0 times and the non-blocking state
    (`state->again`) 0 times.
  - Positive control: upstream commit `570720b` (tag v1.3.1.2) `gzwrite.c`,
    checksum-pinned, shows 5 and 20. The job fails if the control does not
    detect the code.
- **Linkage.** Direct dynamic-symbol imports of `gzwrite`/`gzdopen` were found
  only in `dpkg-deb` and `libapt-pkg`. No inspected file imports `gzprintf`,
  `gzvprintf`, `gzputs`, `gzputc`, `gzfwrite` or `gzopen`. The recorded backup
  and restore used Python's `gzip` over `zlib` streams.
- **Assumptions and disagreement.**
  - The installed binary (`+b1` binNMU) is assumed to be built from that
    source. It was not rebuilt and compared.
  - No zlib version strings were found outside `libz`, but that does not exclude
    stripped or static copies.
  - The Debian tracker, rechecked 2026-09-29 04:58 UTC, still lists bookworm
    (1.2.13), trixie (1.3.1) and forky/sid (1.3.2) as *vulnerable*, and the
    scanner will keep matching. This report does not claim the tracker is wrong.
    It records a disagreement between the advisory's version range plus the
    inspected source, and the tracker's package status.
- **Proposed disposition (candidate-specific):** *not affected: vulnerable code
  absent from the inspected source.* It does not suppress the finding, add an
  exception or close F4b. Revisit on a base or zlib package change, a new
  code path using `gz*` write APIs, or an advisory or tracker change.
  Contacting Debian is external communication and has **not** been authorized
  or done.

### G4 zlib: CVE-2026-27171 (Medium, `crc32_combine64` CPU loop)

No direct dynamic-symbol import of any `crc32_combine*` function was found in the
814 inspected files. Runtime-resolved and intra-library calls are not excluded.
The recorded `ctypes.dlopen` calls were `libllama.so` and the process itself,
both from the llama.cpp bindings. Proposed: provisional low priority, unchanged
from §3.

### G5 glibc

| Advisories | Entry points | Direct dynamic-symbol importers (inspected files) | Proposed status |
| --- | --- | --- | --- |
| CVE-2026-19499 (High) | `strfmon`, `strfmon_l` | none found | no direct import found; runtime-resolved use not excluded |
| CVE-2026-5435 (High), CVE-2026-6238 | `ns_printrr`/`fp_nquery` family | none found (`ns_sprintrr*` exported by `libresolv`, not imported) | no direct import found; runtime-resolved use not excluded |
| CVE-2026-6791, CVE-2026-6368 | `wordexp`, `wordfree` | none found | no direct import found; runtime-resolved use not excluded |
| CVE-2026-18374 | `fopen` with a `,ccs=` mode | `fopen` is universal; no `,ccs=` string outside libc (string search) | no caller passing the mode found; not excluded |
| CVE-2026-19542 | `tdelete` | `libncursesw`, `libtinfo` | ncurses not loaded by the recorded processes (not proven for all paths) |
| CVE-2026-77117, CVE-2026-80489 | `iconv` with EUC/SHIFT_JISX0213 | `bash`, `tar`, diffutils, `iconv`, `libapt-pkg`, **`libstdc++`** (loaded with llama.cpp); JISX0213 gconv modules present | **open**: needs a conversion from a JISX0213 charset. CPython's codecs are its own, but `libstdc++`/llama.cpp conversions are unassessed |
| CVE-2026-89092 | `nscd` | `nscd` not installed | not applicable to this image |
| CVE-2026-86805, CVE-2026-95818 | `ld.so` handling for setuid/setgid (AT_SECURE) programs | 11 setuid/setgid programs present (F1 inventory) | F1's `no-new-privileges` **reduces** this: executing a setuid program no longer changes privileges, so the privileged loader path is not available to the engine user under that configuration (reasoning, not a test of these CVEs). Not a claim about other escalation paths or kernel flaws |
| CVE-2026-8674 | resolver search list | needs control of `resolv.conf`/`LOCALDOMAIN` | unchanged (operator/runtime owned) |
| CVE-2010-4756, CVE-2018-20796, CVE-2019-9192 (Negligible) | `glob`, `regexec`/`regcomp` | shell, tar, grep, apt, PAM modules | tools not run in the recorded sessions (the only spawn observed was `uname -p`) |

### G1 CPython 3.12.14, per module

| Advisory (severity) | Module/function | Static (parsed installed code) | Recorded sessions | Proposed status |
| --- | --- | --- | --- | --- |
| CVE-2026-82049 (High), 19672, 87910 | `tarfile` extraction | engine `backup.py`; pip | `extractall` only in `restore` (operator-run CLI, operator-supplied archive, `_safe_members` + `filter="data"` as §3) | unchanged: operator CLI path; not network-reachable in the engine |
| CVE-2025-15366, CVE-2025-15367 | `imaplib`, `poplib` | no import found in the 1,432 parsed files | not loaded | not used by the parsed installed code; compiled extensions not scanned |
| CVE-2026-15806 | `urllib.request.HTTPPasswordMgr` | referenced only in `pip/_vendor/distlib/index.py` | never constructed | not used by the engine's code paths found |
| CVE-2026-6019 | `http.cookies.Morsel.js_output` | `http.cookies` imported by starlette (and pip); no `set_cookie`/`SimpleCookie`/`js_output` in `engine/` | loaded by starlette; `js_output`, `BaseCookie.load`: 0 calls | not observed; other starlette paths unassessed |
| CVE-2026-15310 (Low) | `zipfile` decompression | Jinja2, numpy, pip | one `ZipFile()` per process from `importlib.metadata` probing a `sys.path` zip entry; `ZipFile.open`: 0 calls | no decompression observed |
| CVE-2026-17084 | `stringprep` via the `idna` codec | pip only | loaded when uvicorn creates its listener; `ToASCII`, `in_table_b2`: 0 calls | **open**: non-ASCII hostnames in operator-supplied model-download or webhook URLs would use IDNA 2003. Not exercised |
| CVE-2025-12781, CVE-2026-3446 | `base64` decoding laxity | engine webhook signing; websockets handshake, fastapi HTTP basic, starlette sessions, pydantic types, llama_cpp chat format, psutil, PyYAML | 0 decode calls | **open**: the engine uses neither starlette `SessionMiddleware` nor fastapi `HTTPBasic` (source search), which leaves the WebSocket handshake key and other unexercised paths. The consequence (lenient decoding) was not assessed for those paths |
| CVE-2026-3479 (Negligible, disputed) | `pkgutil.get_data` | not assessed | 0 calls | unchanged |

### G3 diskcache (GHSA-w8v5-vhqr-4h9v)

- `diskcache` **is imported** whenever `llama_cpp` loads
  (`llama_cpp/llama.py` → `llama_cpp/llama_cache.py`).
- The identified cache-construction paths (`LlamaDiskCache`, `set_cache`)
  appear only in `llama_cpp/server/model.py`, the optional llama-cpp server,
  which the engine does not use.
- In the exercised session there was no tracked construction or use of
  `diskcache.Cache`, `FanoutCache`, `LlamaDiskCache` or `Llama.set_cache`.

Proposed: scoped non-applicability for the inspected engine configuration and
serving path. Reassess if cache configuration, dependencies or serving paths
change. The package stays installed.

### G12 SQLite 3.46.1 (provisional)

Session support **is** compiled in (`ENABLE_SESSION`, `ENABLE_PREUPDATE_HOOK`;
`libsqlite3` exports `sqlite3session_*`, `sqlite3changeset_*` and
`sqlite3changegroup_*`), which corrects the §3 assumption. Extension loading is
also compiled in, and Python exposes `enable_load_extension`/`load_extension`.
No inspected file imports a session, changeset or changegroup function directly
(CPython's `_sqlite3` included), and Python 3.12's `sqlite3.Connection` has no
session API. No parsed installed code calls `load_extension`, and no
`sqlite3.enable_load_extension`/`load_extension` audit event occurred. These
facts narrow exposure. They do not prove that no path to these functions exists,
because runtime-resolved or intra-library use is not excluded.

| Advisory | Prerequisite | Evidence | Proposed status |
| --- | --- | --- | --- |
| CVE-2026-50812 | a malformed changeset applied via `sqlite3changeset_apply_v3` | `apply_v3` is not defined in this library; other apply entry points are exported but not directly imported | provisional: named entry point absent |
| CVE-2026-50813 | changeset concat/changegroup merge on attacker data | exported, no direct importer, no Python API | provisional: no exposure found in inspected code |
| CVE-2025-70873 | the `zipfile` extension processing a crafted ZIP | no load_extension use found; whether the extension is built into this library was not checked | provisional |
| CVE-2021-45346 (Negligible, disputed upstream) | a crafted (modified) database file, then a query against it | the database sits on the engine's volume; modifying it needs write access to that volume. The advisory's consequence is reading memory beyond a record (information disclosure) in the process that queries it | provisional; not dismissed on the write-access prerequisite alone |

### G6 and privileges (F1)

- **Inventory:** the tested image has 11 setuid/setgid files (setuid root:
  `chfn`, `chsh`, `gpasswd`, `mount`, `newgrp`, `passwd`, `su`, `umount`;
  setgid shadow: `chage`, `expiry`, `unix_chkpwd`) and none with file
  capabilities.
- **Docker defaults:** the engine user has no effective capabilities, but
  `CapBnd` is the default set and `NoNewPrivs: 0`.
- **#45's Compose settings** (merged as `199c4da`): every capability set is `0` and `NoNewPrivs: 1`,
  and the full image E2E (including backup, isolated database deletion and
  restore) and the ownership/recovery E2E pass under them.

This reduces the escalation opportunities discussed in G6, G9 and the G5 `ld.so`
items for that configuration. It does not remove the packages or address
every escalation path or kernel vulnerability. #45 is merged, so the settings
are in the repository's release Compose file. A deployment gains them only when
its configuration is updated, which is separate deployment work that has not
been done.

### New since the triage scan: CVE-2026-97399 (Low, glibc)

The #46 dry run on 2026-09-29 (grype DB built 2026-09-29T06:32Z) matched a new
advisory to `libc6` and `libc-bin`, whose versions are unchanged. It moved the Low
count from 10 to 12. Per its description, the affected code is glibc's
`strncasecmp` **optimized for Power8**. This image is linux/amd64, so that
implementation is not selected on it. Proposed: not applicable by architecture
(from the advisory text; the image was not inspected for this item). The fix
state is not-fixed. Not accepted by the user yet.

### Still open

- Runtime-resolved (`dlsym`), intra-library, and stripped or static uses of the
  affected native functions (G4, G5). `libstdc++`/llama.cpp `iconv` with
  JISX0213 charsets (G5).
- Unexercised request paths and settings: WebSocket handshake base64, non-ASCII
  hostnames (IDNA) in model-download or webhook URLs, remote backends, webhook
  delivery, model downloads, and compiled extension modules (G1, G3, G12).
- Whether the installed `zlib1g` binary matches the inspected source, and the
  tracker disagreement (G4, F4b).
- SQLite per-advisory items above (G12).

### Revisited in A3b (2026-09-29, draft PR #48)

A3b exercises the webhook administration paths. Two concrete findings there are
fixed in #48 and take effect only once it is merged:

- **Webhook redirects bypassed `egress_allowlist`.** The delivery transport used
  the default `urllib` opener, which follows 301/302/303 redirects. The
  allowlist is checked only when an endpoint is registered, so an allowlisted
  receiver that redirected made the engine send a follow-up request (GET, with
  the `webhook-id`/`-timestamp`/`-signature` headers) to an unchecked host,
  including loopback or internal addresses. The delivery was then recorded as
  succeeded. Shown by the pre-fix run (CI 36630287301). Fixed: redirects are
  never followed, and a 3xx is a failed attempt. Prerequisite for exploitation:
  a registered, allowlisted receiver that returns a redirect.
- **IDNA for webhook hosts.** A non-ASCII host that is a subdomain of an
  allowlisted entry (for example `bücher.hooks.example.com`) was accepted, and
  delivery would then go through the IDNA 2003 conversion (G1 CVE-2026-17084).
  Fixed: endpoint hosts must be ASCII (`endpoint_host_not_ascii`; use the
  `xn--` form). Endpoints registered earlier are unchanged.
- **Audit.** Billing and webhook admin writes, including creating a client key,
  were not audited. They are now, with ids, labels and hosts only.

Still open after A3b (tracked, not urgent on the assessed evidence):

- **Model downloads** (`POST /admin/models/download`, operator-only, gated by
  `allow_network_downloads`) have no host allowlist and follow redirects by
  design, because Hugging Face serves files through CDN redirects. Non-ASCII
  download hosts still go through IDNA 2003.
- Narrowing `egress_allowlist` does not disable webhook endpoints that are
  already registered (documented in `docs/webhooks.md`).
- WebSocket handshake base64, runtime-resolved native calls, `libstdc++`
  `iconv`, remote backends and compiled extensions are unchanged.

## 3b. Proposed versus accepted dispositions

| Item | Proposed (this report) | User-accepted |
| --- | --- | --- |
| G4 CVE-2026-85091 | not affected: vulnerable code absent from the inspected source (candidate-specific) | not yet |
| G3 diskcache | scoped non-applicability for the inspected configuration | not yet |
| G12 SQLite | provisional per advisory (see table) | not yet |
| G1 imaplib/poplib/HTTPPasswordMgr | not used by the parsed installed code | not yet |
| CVE-2026-97399 (new Low) | not applicable by architecture (Power8-only code; image is amd64) | not yet |
| Everything else in §3a | status as listed; open items stay open | not yet |

## 4. Decision for A3a (2026-09-28, historical)

**No urgent blocker identified in the assessed findings, with explicit
unresolved work.** This is an input to the A3a entry decision, not the decision
itself: A3a starts only after #41 is user-merged, this report is reviewed, and
the user authorizes the transition. Reasons:

- There are no Critical findings, and no advisory matched the scanner's KEV
  feed. No-KEV and low EPSS are priority inputs, not proofs of safety.
- The High advisories with a network-reachable component are G1 and G4. For
  G1, the only affected module the engine uses (`tarfile`) runs from an operator
  CLI with member-type filtering that rejects the crafted link shapes. For G4,
  no use of the affected zlib `gzFile` path is known. That is an **assumption**
  about CPython and native wheels, not an observed absence (F4a). A fix exists
  upstream but not as a Debian package.
- Most High matches (G6–G9) are userland tools the engine does not run. Their
  real risk is escalation after a compromise, which F1 addresses.

What is **not** established: reachability of the glibc (G5) and zlib (G4)
functions through CPython or native dependencies; third-party use of the
affected stdlib modules (G1); diskcache defaults (G3); the SQLite Session
conclusion (G12); the setuid state of this image (G6). A lack of any known fix
does not by itself make an item low risk. Production assurances need F1–F6
assessed and an explicit risk decision by the user.

## 4a. Reassessment before A3b (2026-09-29)

**No urgent blocker has been identified in the assessed findings; unresolved
investigations may change that decision.** This is an input to the A3b entry
decision. The decision and any risk acceptance are the user's.

- The High with a plausible network-reachable library path (G4 CVE-2026-85091)
  is absent from the inspected zlib source. That rests on the source-to-binary
  assumption and disagrees with the Debian tracker.
- The other High in a used module (G1 CVE-2026-82049) stays on the operator-run
  `restore` CLI path.
- No direct dynamic-symbol imports of the glibc High entry points (`strfmon`,
  `ns_printrr`/`fp_nquery`) were found in the inspected files. The
  util-linux/perl/acl/ncurses Highs are in tools not run in the recorded
  sessions, and #45 (merged) reduces their escalation value for deployments that
  adopt the updated Compose file.
- The items under "Still open" are **not** all low impact or operator-only.
  Several are simply unassessed. None of the assessed evidence points to an
  exposure that should take priority over A3b. A3b is expected to exercise some
  of these paths (webhook delivery, URL handling), which will be revisited then.
- This is not a vulnerability-free claim, a production approval or a statement
  that the follow-ups are complete. The passing gate is a policy result (no
  unexcepted Critical), not an exploitability assessment.

## 5. Follow-ups

| ID | Next bounded work | Proposed owner (not yet accepted) | Status | Depends on | Required evidence / review point |
| --- | --- | --- | --- | --- | --- |
| F1 | List the image's setuid/setgid files and effective capabilities; propose Compose `security_opt: [no-new-privileges:true]` + `cap_drop: [ALL]` with matching docs (read-only rootfs/tmpfs is optional extra scope) | Claude implements after user authorization; user decides | **integrated**: #45 merged 2026-09-29 as `199c4da`; main CI 36565420928 green (attempt 1). PR qualification: CI 36514494237, Release 36514494606. **Not deployed**: existing deployments need a separately authorized configuration update. Read-only rootfs and the development compose file are not in scope | user authorization | Separate PR; image E2E under the hardened configuration incl. volume ownership/persistence; before v0.2.0 release |
| F2 | Evaluate removing pip from the runtime layer after `pip check`, or hash-locking a patched pip | Claude after authorization; user decides | not started | user authorization | Separate image change; package inventory, re-scan, Release E2E; next base refresh |
| F3 | Bounded Actions investigation: third-party use of `imaplib`/`poplib`/`http.cookies`/`zipfile`/`stringprep`; llama-cpp-python disk-cache defaults; SQLite Session exposure (G12) and the two SQLite Negligibles | Claude after authorization; user reviews | **partially investigated** (§3a, draft PR #47, audit 36523922216). Open: base64/WebSocket, IDNA, compiled extensions, unexercised request paths; G3/G12 proposals await the user | none | Explicit reachability evidence plus remaining unknowns; before release review |
| F4a | Native/CPython reachability audit of the affected zlib (`gzwrite`/`gz_vacate`, `crc32_combine64`) and glibc functions in the image's shared objects | Claude after authorization; user reviews | **partially investigated** (§3a, draft PR #47, audit 36523922216). Open: runtime-resolved/intra-library calls, stripped or static copies, `libstdc++` iconv, binary-to-source identity; G4 proposal awaits the user | none (does not wait for a package fix) | Source/package-specific evidence; before release review |
| F4b | Monitor for a fixed Debian zlib package (bug #1146895) and the glibc/util-linux items | user/operator (proposed) | **open; monitoring not scheduled**. Proposed cadence: weekly tracker check (rechecked 2026-09-29: trixie still *vulnerable*), plus a re-dispatch of the image audit at each base refresh. It is not closed by an internal disposition. Contacting Debian needs explicit authorization | Debian | Weekly while the zlib High is unfixed |
| F5 | When a newer CPython 3.12 or `python:3.12-slim` digest exists, verify each G1 advisory against its release notes, re-scan and compare | user/operator (proposed); Claude can prepare | waiting on upstream | a new release/digest | Advisory-by-advisory fix list, scan diff, compatibility + Release gates |
| F6 | Retain SBOM + full grype JSON as a bounded-retention artifact on non-publishing Release runs | Claude after authorization; user decides | **integrated**: #46 merged 2026-09-29 as `84b6817` (merge commit; parent `6c183e9`); main CI [36612186495](https://github.com/dlroqa/inference-engine/actions/runs/36612186495) green (push, attempt 1, 11/11 jobs). PR qualification on `main`: CI 36567830306 and Release 36567830756 (checkout `3dc8a8a`; publish skipped; 53 PASS/0 FAIL; artifact 11032663606 checksums verified). Earlier: dry run 36514562548; combined F1+F6 stack 36523517754. **Limitation:** retention after a *failing* Critical gate is configured and structurally tested but has not been exercised by an actual failing-gate run. Nothing is deployed by this | user authorization | Scoped workflow PR (minimal permissions, publication guard unchanged) + a dry run proving retention |

## 6. Revision log

- 2026-09-28 (review corrections): KEV claim scoped to the scanner feed; G12
  SQLite downgraded from justified to provisional/investigate; CPython/native
  reachability uncertainty carried into G5 and §4; F4 split into an audit that
  proceeds now (F4a) and package monitoring (F4b); zlib upstream fix vs. absent
  Debian package distinguished (tracker rechecked 2026-09-28); no assumption that
  a future 3.12 release fixes all G1 items (python.org rechecked 2026-09-28: 3.12.14
  still newest); owners marked proposed, not accepted. Inventory, evidence
  identity, grouping and the Critical-only gate policy unchanged.
- 2026-09-29 (F1/F3/F4a/F6 evidence): added §3a (image reachability audit, F1
  privilege inventory) and §4a (reassessment before A3b). Corrected §1: the
  engine reaches `subprocess` indirectly (`platform.processor()` → `uname -p`).
  G6 wording changed from "Not an A3a blocker" to "No urgent blocker has been
  identified in the assessed findings; unresolved investigations may change that
  decision." Corrected the G12 assumption: Session support is compiled into
  `libsqlite3`, but nothing imports it. Proposed, not accepted: G4 85091 not
  affected (code absent), G3 and G12 justified non-applicability. F1/F6 linked to
  draft PRs #45/#46. §3 table, the appendix, the gate policy and the exceptions
  are otherwise unchanged. F2–F6 remain open.
- 2026-09-29 (review corrections, second pass): the audit now fails on
  incomplete inspection and checks the recorded workload and its outcomes; the
  Debian source check has a signed trust chain and a pinned positive control
  (run 36523922216; raw evidence retained under `docs/security/evidence/`).
  Earlier audit runs are labelled as unenforced observations. Wording: "no
  direct dynamic-symbol imports found in the inspected files" replaces "not
  reachable by dynamic linkage". The G4 disposition is scoped as candidate-specific,
  with the tracker disagreement (rechecked 2026-09-29) and the binary assumption
  kept. G3 is bounded to the inspected configuration and session. G12 is back to
  provisional per advisory; the "extension loading is the only path" claim and
  the write-access dismissal are removed. F1 "reduces", it does not "address".
  Residual items are no longer called low impact. Added §3b (proposed versus
  accepted) and a current-state summary. The §3 table and §4 are marked as the
  2026-09-28 record.
- 2026-09-29 (integration status): #45 merged as `199c4da` (main CI
  36565420928 green), so F1 is recorded as integrated, not deployed. #46 was
  retargeted to `main`. F-statuses are reported individually instead of
  "F1–F6 open". #46 was requalified on `main` (Release 36567830756). Added
  CVE-2026-97399, a new Low matched by the 2026-09-29 grype database, with a
  proposed architecture-based non-applicability. Other evidence, findings and
  dispositions are unchanged.
- 2026-09-29 (integration status): #46 merged as `84b6817` (main CI
  36612186495 green), so F6 is recorded as integrated, with the failing-gate
  limitation kept. Findings and dispositions are unchanged; no disposition has
  been accepted by the user.

## Appendix: every advisory in the fresh scan

Generated from `grype.json` of run 36317166186. "Matches" is the number of
installed packages the advisory matched. The fix state is grype's, from the
Debian tracker for `deb` packages and from GitHub/NVD for Python.

| Advisory | Severity | Group | Packages (installed version) | Matches | Fix state (grype) | EPSS |
| --- | --- | --- | --- | ---: | --- | ---: |
| CVE-2025-69720 | High | G7 | libncursesw6@6.5+20250216-2 and 3 more (libtinfo6, ncurses-base, ncurses-bin) | 4 | wont-fix | 0.0045 |
| CVE-2026-19499 | High | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0030 |
| CVE-2026-5435 | High | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0039 |
| CVE-2026-54369 | High | G9 | libacl1@2.3.2-2+b1 | 1 | wont-fix | 0.0019 |
| CVE-2026-54370 | High | G9 | libacl1@2.3.2-2+b1 | 1 | wont-fix | 0.0011 |
| CVE-2026-76642 | High | G6 | bsdutils@1:2.41.5-0+deb13u1 and 8 more (libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux) | 9 | wont-fix | 0.0022 |
| CVE-2026-78408 | High | G6 | bsdutils@1:2.41.5-0+deb13u1 and 8 more (libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux) | 9 | wont-fix | 0.0019 |
| CVE-2026-78409 | High | G6 | bsdutils@1:2.41.5-0+deb13u1 and 8 more (libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux) | 9 | wont-fix | 0.0015 |
| CVE-2026-78410 | High | G6 | bsdutils@1:2.41.5-0+deb13u1 and 8 more (libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux) | 9 | wont-fix | 0.0016 |
| CVE-2026-82049 | High | G1 | python@3.12.14 | 1 | fixed 3.14.0b1 | 0.0019 |
| CVE-2026-82560 | High | G8 | perl-base@5.40.1-6+deb13u1 | 1 | not-fixed | 0.0063 |
| CVE-2026-85091 | High | G4 | zlib1g@1:1.3.dfsg+really1.3.1-1+b1 | 1 | not-fixed | 0.0059 |
| CVE-2026-9538 | High | G8 | perl-base@5.40.1-6+deb13u1 | 1 | wont-fix | 0.0056 |
| CVE-2025-12781 | Medium | G1 | python@3.12.14 | 1 | fixed 3.13.10, 3.14.1, 3.15.0a2 | 0.0057 |
| CVE-2025-15366 | Medium | G1 | python@3.12.14 | 1 | fixed 3.13.15, 3.14.7, 3.15.0a6 | 0.0042 |
| CVE-2025-15367 | Medium | G1 | python@3.12.14 | 1 | fixed 3.15.0a6 | 0.0037 |
| CVE-2025-6141 | Medium | G7 | libncursesw6@6.5+20250216-2 and 3 more (libtinfo6, ncurses-base, ncurses-bin) | 4 | wont-fix | 0.0020 |
| CVE-2026-15059 | Medium | G11 | libsystemd0@257.13-1~deb13u1, libudev1@257.13-1~deb13u1 | 2 | wont-fix | 0.0016 |
| CVE-2026-15534 | Medium | G8 | perl-base@5.40.1-6+deb13u1 | 1 | wont-fix | 0.0026 |
| CVE-2026-15806 | Medium | G1 | python@3.12.14 | 1 | fixed 3.15.0rc2 | 0.0045 |
| CVE-2026-16742 | Medium | G11 | libsystemd0@257.13-1~deb13u1, libudev1@257.13-1~deb13u1 | 2 | wont-fix | 0.0006 |
| CVE-2026-17084 | Medium | G1 | python@3.12.14 | 1 | fixed 3.15.0rc2 | 0.0072 |
| CVE-2026-18374 | Medium | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0014 |
| CVE-2026-18477 | Medium | G13 | tar@1.35+dfsg-3.1 | 1 | wont-fix | 0.0008 |
| CVE-2026-18508 | Medium | G13 | tar@1.35+dfsg-3.1 | 1 | wont-fix | 0.0014 |
| CVE-2026-19487 | Medium | G8 | perl-base@5.40.1-6+deb13u1 | 1 | wont-fix | 0.0042 |
| CVE-2026-19542 | Medium | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0023 |
| CVE-2026-19672 | Medium | G1 | python@3.12.14 | 1 | unknown | 0.0052 |
| CVE-2026-27171 | Medium | G4 | zlib1g@1:1.3.dfsg+really1.3.1-1+b1 | 1 | wont-fix | 0.0019 |
| CVE-2026-3184 | Medium | G6 | bsdutils@1:2.41.5-0+deb13u1 and 8 more (libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux) | 9 | wont-fix | 0.0062 |
| CVE-2026-3446 | Medium | G1 | python@3.12.14 | 1 | fixed 3.13.13, 3.14.4, 3.15.0a8 | 0.0022 |
| CVE-2026-42250 | Medium | G14 | libbz2-1.0@1.0.8-6 | 1 | wont-fix | 0.0018 |
| CVE-2026-50812 | Medium | G12 | libsqlite3-0@3.46.1-7+deb13u2 | 1 | wont-fix | 0.0016 |
| CVE-2026-50813 | Medium | G12 | libsqlite3-0@3.46.1-7+deb13u2 | 1 | wont-fix | 0.0016 |
| CVE-2026-54371 | Medium | G9 | libattr1@1:2.5.2-3 | 1 | wont-fix | 0.0018 |
| CVE-2026-54411 | Medium | G10 | libpam-modules-bin@1.7.0-5 and 3 more (libpam-modules, libpam-runtime, libpam0g) | 4 | wont-fix | 0.0050 |
| CVE-2026-5704 | Medium | G13 | tar@1.35+dfsg-3.1 | 1 | wont-fix | 0.0040 |
| CVE-2026-6019 | Medium | G1 | python@3.12.14 | 1 | fixed 3.13.14, 3.14.5rc1, 3.15.0b1 | 0.0058 |
| CVE-2026-6238 | Medium | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0044 |
| CVE-2026-6791 | Medium | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0033 |
| CVE-2026-77117 | Medium | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0041 |
| CVE-2026-80489 | Medium | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0041 |
| CVE-2026-8674 | Medium | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0034 |
| CVE-2026-86805 | Medium | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0012 |
| CVE-2026-87910 | Medium | G1 | python@3.12.14 | 1 | unknown | 0.0054 |
| CVE-2026-89092 | Medium | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0027 |
| GHSA-4xh5-x5gv-qwph | Medium | G2 | pip@25.0.1 | 1 | fixed 25.3 | 0.0047 |
| GHSA-58qw-9mgm-455v | Medium | G2 | pip@25.0.1 | 1 | fixed 26.1 | 0.0018 |
| GHSA-jp4c-xjxw-mgf9 | Medium | G2 | pip@25.0.1 | 1 | fixed 26.1 | 0.0017 |
| GHSA-qwm4-qh6w-59xr | Medium | G2 | pip@25.0.1 | 1 | fixed 26.2.0 | 0.0029 |
| GHSA-w8v5-vhqr-4h9v | Medium | G3 | diskcache@5.6.3 | 1 | not-fixed | 0.0053 |
| GHSA-wf93-45jw-7689 | Medium | G2 | pip@25.0.1 | 1 | fixed 26.1.2 | 0.0047 |
| CVE-2024-56433 | Low | G15 | login.defs@1:4.17.4-2, passwd@1:4.17.4-2 | 2 | wont-fix | 0.0042 |
| CVE-2026-15310 | Low | G1 | python@3.12.14 | 1 | fixed 3.15.0rc2 | 0.0050 |
| CVE-2026-40228 | Low | G11 | libsystemd0@257.13-1~deb13u1, libudev1@257.13-1~deb13u1 | 2 | wont-fix | 0.0014 |
| CVE-2026-6368 | Low | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0015 |
| CVE-2026-95818 | Low | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | wont-fix | 0.0013 |
| GHSA-6vgw-5pg2-w6jp | Low | G2 | pip@25.0.1 | 1 | fixed 26.0 | 0.0041 |
| CVE-2005-2541 | Negligible | G13 | tar@1.35+dfsg-3.1 | 1 | not-fixed | 0.0399 |
| CVE-2007-5686 | Negligible | G15 | login.defs@1:4.17.4-2, passwd@1:4.17.4-2 | 2 | not-fixed | 0.0094 |
| CVE-2010-4756 | Negligible | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | not-fixed | 0.0261 |
| CVE-2011-3374 | Negligible | G16 | apt@3.0.3, libapt-pkg7.0@3.0.3 | 2 | not-fixed | 0.0119 |
| CVE-2011-4116 | Negligible | G8 | perl-base@5.40.1-6+deb13u1 | 1 | not-fixed | 0.0052 |
| CVE-2013-4392 | Negligible | G11 | libsystemd0@257.13-1~deb13u1, libudev1@257.13-1~deb13u1 | 2 | not-fixed | 0.0047 |
| CVE-2017-18018 | Negligible | G16 | coreutils@9.7-3 | 1 | not-fixed | 0.0034 |
| CVE-2018-20796 | Negligible | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | not-fixed | 0.0576 |
| CVE-2019-1010022 | Negligible | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | not-fixed | 0.0322 |
| CVE-2019-1010023 | Negligible | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | not-fixed | 0.0304 |
| CVE-2019-1010024 | Negligible | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | not-fixed | 0.0319 |
| CVE-2019-1010025 | Negligible | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | not-fixed | 0.0227 |
| CVE-2019-9192 | Negligible | G5 | libc-bin@2.41-12+deb13u4, libc6@2.41-12+deb13u4 | 2 | not-fixed | 0.0243 |
| CVE-2021-45346 | Negligible | G12 | libsqlite3-0@3.46.1-7+deb13u2 | 1 | not-fixed | 0.0161 |
| CVE-2022-0563 | Negligible | G6 | bsdutils@1:2.41.5-0+deb13u1 and 8 more (libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux) | 9 | not-fixed | 0.0043 |
| CVE-2023-31437 | Negligible | G11 | libsystemd0@257.13-1~deb13u1, libudev1@257.13-1~deb13u1 | 2 | not-fixed | 0.0034 |
| CVE-2023-31438 | Negligible | G11 | libsystemd0@257.13-1~deb13u1, libudev1@257.13-1~deb13u1 | 2 | not-fixed | 0.0032 |
| CVE-2023-31439 | Negligible | G11 | libsystemd0@257.13-1~deb13u1, libudev1@257.13-1~deb13u1 | 2 | not-fixed | 0.0035 |
| CVE-2025-5278 | Negligible | G16 | coreutils@9.7-3 | 1 | not-fixed | 0.0029 |
| CVE-2025-70873 | Negligible | G12 | libsqlite3-0@3.46.1-7+deb13u2 | 1 | not-fixed | 0.0030 |
| CVE-2026-3479 | Negligible | G1 | python@3.12.14 | 1 | fixed 3.13.13, 3.14.4, 3.15.0a8 | 0.0024 |
| CVE-2026-53910 | Negligible | G16 | diffutils@1:3.10-4 | 1 | not-fixed | 0.0033 |
| CVE-2026-56391 | Negligible | G16 | coreutils@9.7-3 | 1 | not-fixed | 0.0017 |
| CVE-2026-56392 | Negligible | G16 | coreutils@9.7-3 | 1 | not-fixed | 0.0019 |
