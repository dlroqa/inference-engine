# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Each released version needs a
non-empty `## [X.Y.Z]` section: the release workflow copies it into the GitHub
release notes and refuses to publish without it (see `docs/releasing.md`).

## [Unreleased]

### Security

- Operator surfaces (`/admin/*`, `/metrics`, `/logs`, `/diagnostics`, `/ws/*`)
  now reject keys owned by a billing client with `403 operator_role_required`,
  and evaluate presented credentials before the loopback exception: invalid or
  revoked keys are refused from localhost too, and keyless loopback access is
  allowed only while effective authentication is off. Previously any valid key,
  including a client's, opened every operator endpoint.
- Credential query values (e.g. the dashboard's WebSocket `?api_key=`) are
  redacted from server log lines and the stored log buffer.

### Added

- `GET /admin/identity` (current operator key metadata or local-dev identity,
  never a token) and `GET /admin/system` (read-only build, readiness, feature
  switches as booleans, and a redacted routing summary).
- API key listings include `role` (`operator`/`client`) and `client_id`.
- Model responses redact credentials from `source_ref` URLs.
- Dashboard: shareable URLs (hash routing) with working back/forward, an
  identity/key menu with change and forget, distinct screens for a missing key,
  a client key, and an unreachable engine, confirmation dialogs for destructive
  actions, a server-side request-id log filter, per-endpoint webhook deliveries,
  and a visible error when an audit verification request fails.
- Dashboard: model actions that a feature switch disables
  (`allow_model_management`, `allow_network_downloads`) are shown disabled with
  the switch named, using `GET /admin/system`. Feature 403s name their switch
  and stay distinct from authorization failures. If the switch status cannot be
  read, the dashboard says so, offers Retry, and leaves enforcement to the engine.
- Dashboard: a "Show wiring" toggle (off by default, remembered in the browser)
  that shows each control's `METHOD /path` beside its "How this works" hint.

## [0.1.1] - 2026-09-25

First published prebuilt image. `v0.1.0` was tagged, but its publish job stopped
before any versioned image tag or GitHub release was created, so 0.1.1 is the
first release to deploy; it contains everything listed under 0.1.0 below.

### Added

- A release `install.sh` asset for Linux x86-64 AVX2 hosts. It verifies the
  release Compose file and settings template against `SHA256SUMS` before pulling
  and starting the digest-pinned prebuilt image; the documented separate-download
  path also verifies the installer itself before execution.

### Fixed

- Release publishing: the registry login now uses `docker login`, so the
  provenance and SBOM attestations can be pushed to GHCR alongside the image
  (a `skopeo login` left the attest actions without registry credentials).

### Upgrade notes

- No new database migrations; migrations `0001`–`0008` remain forward-only.

## [0.1.0] - 2026-09-24

First prebuilt release. Supported deployment: the `linux/amd64` CPU image on a
Linux x86-64 host with AVX2.

### Added

- Prebuilt CPU container image `ghcr.io/dlroqa/inference-engine` with the local
  llama.cpp backend (`llama-cpp-python` 0.3.35, CPU wheel) always installed, the
  FastAPI engine, database migrations, and the operator dashboard at `/dashboard`.
- Owner-facing Compose template (`deploy/compose.yaml`) pinned to an immutable
  image digest in each release, plus `deploy/inference-engine.env.example`.
- Tag-triggered release workflow: quality gates, real GGUF integration, one image
  build, image-level end-to-end test, SBOM, vulnerability gate, GitHub OIDC
  provenance/SBOM attestations, promote-by-digest publishing, and GitHub release.
- Hash-locked runtime dependencies for the image (`requirements/release-cpu.txt`)
  and digest-pinned base images.

### Changed

- The package version now has a single source (`engine.__version__`) that
  `pyproject.toml` reads; it moves from the `0.0.0` placeholder to `0.1.0`.
- The Dockerfile no longer takes `INSTALL_LLAMA`; the CPU backend is unconditional.

### Upgrade notes

- First release: there is no earlier release to roll back to. Database
  migrations `0001`–`0008` are forward-only.
