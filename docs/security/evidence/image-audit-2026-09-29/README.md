# Image reachability audit evidence (2026-09-29)

Raw evidence behind §3a of `docs/security/candidate-image-triage.md`, copied
unchanged from a GitHub Actions artifact so it outlives the artifact's 14-day
retention. Nothing here was produced locally.

| Field | Value |
| --- | --- |
| Workflow run | [Image reachability audit 36523922216](https://github.com/dlroqa/inference-engine/actions/runs/36523922216), attempt 1, event `pull_request` (PR #47) |
| Checkout | synthetic merge `c6d9544751eb6767129f58bf7c461593ebf578d2` of PR head `6f042d6399c50d4de5afd359e9d14f5d3401dca3` into `main` `f121e91` |
| Artifact | `image-reachability-audit`, id 11014051335, 115,574 bytes (zip), digest `sha256:24c2173b830e51cad865d560b1c67ae6c516962183c5cf1a18c8bf1e4d601c2e`, expires 2026-10-13T04:58:44Z |
| Integrity | every file matches `SHA256SUMS`, which the run wrote and printed in its job summary |
| Candidate | rebuilt, not the scanned or any published image: see `candidate.json` (manifest, config, input checksums, tools) |

Files:

- `candidate.json`: build identity, probe/Dockerfile/lock checksums, tool versions.
- `dpkg.tsv`: installed Debian packages with source package and version.
- `elf.json`/`elf.md`: dynamic-symbol imports/definitions of the affected entry
  points, with completeness (files examined, ELF discovered/inspected/unassessed).
- `static.json`/`static.md`: installed distributions importing the affected
  stdlib modules (AST), SQLite and zlib runtime identity, with completeness.
- `zlib-source.json` and `zlib-source/`: the Debian zlib source check. The
  trust chain runs from the base image's archive keyring through the signed
  `InRelease` (`gpgv-status.txt`), the Sources index paragraph and `files.sha256`,
  to the `.dsc`, the Debian tarball and the inspected `gzwrite.c`/`zlib.h`, plus
  the pinned upstream positive control `control-gzwrite.c`. The upstream orig
  tarball is not copied here; its SHA-256 is in `zlib-source.json`, and the
  source is durably available at snapshot.debian.org (see `durable_reference`).
- `workload.json`, `session-outcome.json`, `serve-drain-count.txt`: the checked
  serve session, which records statuses, counts and pass/fail only (no prompts,
  generated text or keys), and the serve, backup and restore outcomes.
- `record-serve.json`, `record-backup.json`, `record-restore.json`: modules
  loaded, counted calls, audit events and their calling stacks for each process.
