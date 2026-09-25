# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Each released version needs a
non-empty `## [X.Y.Z]` section: the release workflow copies it into the GitHub
release notes and refuses to publish without it (see `docs/releasing.md`).

## [Unreleased]

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
