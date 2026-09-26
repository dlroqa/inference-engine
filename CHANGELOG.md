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
- Model list and detail responses now redact credentials from the stored
  `error` text too: URL userinfo and credential-like query values, in every URL
  or query parameter the error mentions. Malformed source URLs are redacted
  conservatively instead of failing the request.
- Model download failures are sanitized before they are logged: the
  `model_download_failed` warning's `detail` gets the same credential redaction
  as API responses (URL userinfo, credential-like query values, encoded names,
  nested redirect targets) before the logger is called, so no handler or
  formatter sees the raw text. If redaction fails, a fixed message is logged
  instead. Every ordinary failure of the download task takes this path, including
  invalid URLs (`ValueError`/`InvalidURL`, now a `DownloadError` that does not
  quote the URL), read errors, unexpected worker exceptions and failures while
  probing the finished file: the model is marked `error` and the exception no
  longer escapes to asyncio's unhandled-task report (previously such a model
  stayed `downloading`). The warning now also carries `stage` and `error_type`.
  A database failure while recording the outcome is logged as
  `model_download_state_not_recorded`. The registry still stores the raw error
  text, and log lines written before this change are not rewritten; see
  `docs/security.md`.
- Model download cancellation now waits for the worker thread.
  - A cancel during a blocked read discards whatever the read returns, and the
    checksum stops between chunks.
  - The final rename is a commit point: a cancel accepted before it prevents it,
    and after it, cancel requests are refused (the download finishes).
  - Task cancellation no longer releases a download whose worker is still
    running. The outcome is recorded after the worker stops, then the
    cancellation is re-raised.
  - Shutdown waits until every download worker has stopped. After a 10-second
    grace period it logs `model_download_shutdown_waiting` and keeps waiting
    rather than abandoning the worker, so shutdown can take longer while a
    download is in flight (see `docs/deployment.md`).
- `DELETE /admin/models/{id}` no longer cancels an in-flight download and removes
  its files at once. It answers `409 model_busy` while the engine owns work for
  the model: a download until its worker has stopped (also after an accepted
  cancel and during finalization), a load, or an import of the same file. A
  refused delete changes nothing. Cancel, wait for the download to stop, then
  delete. Loads and imports now also answer `409 model_busy` when the model or
  file is in use by another operation. If deleting files or the row fails, the
  answer is `500 model_delete_failed`, the row is kept, and a repeated delete
  converges. The delete audit event is written only after success.
- `POST /admin/models/{id}/cancel` now answers `409 model_cancel_not_accepted`
  when the engine refuses the request: the file is already committed (the
  download is finishing), or no worker is running for the model. Previously
  it answered `{"cancelling": true}` regardless. An accepted request still
  answers `{"cancelling": true, "id": ...}`, meaning the request was accepted,
  not that the download has already stopped. The dashboard shows the refusal as
  the row's error, and its Cancel explanation now says so.
- The model-source redaction now also removes userinfo containing quotes or
  parentheses, masks credential values containing them, and masks credentials
  inside a nested redirect URL passed as a query value.

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
- Dashboard: if a protected request returns 401 or `operator_role_required`
  (for example, the key was revoked), the dashboard ends the session. Its views
  and switch state are discarded, and the key prompt or operator-access screen
  is shown. A late failure from an earlier session is ignored.
- Dashboard: a "Show wiring" toggle (off by default, remembered in the browser)
  that shows each control's `METHOD /path` beside its "How this works" hint.
- Dashboard: a Scheduler card on the Overview. It shows concurrency in use,
  queue depth, admitted, rejected and cancelled requests, and waits as
  avg / max / last. Energy gives the engine's reason when it is not measured.
- Dashboard: live metrics show a loading state rather than zeros before the
  first data arrives. Data kept across a disconnect is marked as not current.
- Dashboard: a model details drawer (`#/models/<id>`, `GET /admin/models/{id}`)
  with the checksum and a copy button. A local import's path is not shown.
  Back and Forward work, and focus returns to where the drawer was opened.
- Dashboard: client rows can be selected from the keyboard (Enter or Space on
  the client's button).
- `docs/dashboard.md` gains a UI → subsystem → endpoint table generated from
  the wiring registry. CI checks it for drift and prints the regenerated diff.

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
