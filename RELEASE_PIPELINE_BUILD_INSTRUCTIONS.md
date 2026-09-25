# Prebuilt CPU Release Pipeline — Strict Build Instructions

## Purpose

Implement a release pipeline that publishes a **ready-to-run, CPU inference
deployment image**. A server owner must be able to deploy a released version
without building Python, Node, the dashboard, or `llama-cpp-python`.

The primary release artifact is an OCI/Docker image. A Python wheel may also be
published for developers, but it is not the supported server deployment path.

This document is intentionally prescriptive. Follow the order below. Do not
claim a release is complete unless every required verification step passes.

## Scope and non-goals

### In scope

- A prebuilt `linux/amd64` CPU image with the local llama.cpp backend included.
- The existing FastAPI engine, database migrations, and bundled operator
  dashboard at `/dashboard`.
- Versioned image publishing, immutable digests, SBOM/provenance, release notes,
  and a Compose deployment template.
- Image-level validation that proves the published artifact, not merely the
  source checkout, can start and serve a real GGUF generation.

### Explicitly out of scope

- Bundling model files into the image. Models are large, separately licensed,
  and must live in persistent storage under `/data/models` or be downloaded
  through the authenticated model-management flow.
- CUDA, ROCm, or any GPU support claim. Add these later as separate image lines
  after real target-hardware qualification.
- Kubernetes, multi-architecture support, or an installer that modifies a host.
- Replacing existing API-key authentication, dashboard behavior, data layout,
  migration behavior, or security controls.

## Required decisions before implementation

1. Confirm the container registry namespace. Default recommendation:
   `ghcr.io/<GitHub-owner>/inference-engine`.
2. Confirm that stable release tags are protected and that GitHub Actions has
   `packages: write`, `contents: write`, and `attestations: write` permissions.
3. Choose the first release version (for example `v0.1.0`) and create a
   changelog/release-notes convention.
4. State that the first supported deployment platform is Linux x86-64 with AVX2.
   Do not call macOS, Windows, ARM, or GPU deployments supported in this release.

Stop and request direction if the registry namespace or release authority is
unknown. Do not publish to a personal, temporary, or unapproved registry.

## Required release contract

For every stable release `vMAJOR.MINOR.PATCH`, publish:

| Artifact | Requirement |
| --- | --- |
| Container image | `ghcr.io/<owner>/inference-engine:vMAJOR.MINOR.PATCH` |
| Immutable reference | SHA-256 image digest recorded in release notes |
| Convenience tags | `MAJOR.MINOR`, `MAJOR`, and `latest`, updated only after stable-release validation |
| Compose file | References the immutable digest, not a mutable tag |
| SBOM | Attached to the release and associated with the image |
| Provenance/signature | Generated with GitHub Actions OIDC and associated with the pushed image |
| Release notes | Version, commit, build date, digest, platform, requirements, upgrade and rollback steps |

Optionally publish `sha-<commit>` for traceability and `edge` for non-production
testing. Never point `latest` to an untested branch build.

## Implementation steps

### 1. Establish one source of release version truth

1. Replace the placeholder package version (`0.0.0`) with release-managed
   semantic versioning.
2. On a tag build, require the tag form `vMAJOR.MINOR.PATCH` and derive the
   package/image version from it after stripping the leading `v`.
3. Fail the release before building if the tag is malformed or the version is
   inconsistent among package metadata, image labels, `/version`, and release
   notes.
4. Preserve the existing build commit/date values, but populate them during the
   release build from the checked-out tag commit and a UTC RFC 3339 timestamp.
5. Add OCI labels at minimum:

   ```text
   org.opencontainers.image.source
   org.opencontainers.image.revision
   org.opencontainers.image.version
   org.opencontainers.image.created
   org.opencontainers.image.licenses
   ```

Do not use a moving branch name as a release version.

### 2. Make the production Docker image CPU-ready by default

1. Retain the existing multi-stage structure:
   - dashboard build stage;
   - Python wheel build stage, including `engine/static`;
   - minimal non-root runtime stage.
2. The release image **must always install** `llama-cpp-python` using the
   CPU-wheel index. Do not require a server owner to pass `INSTALL_LLAMA=true`.
3. Either remove `INSTALL_LLAMA` from the production Dockerfile or make CPU
   installation unconditional. If a minimal/developer image is retained, give it
   a different explicit target/tag; never make it the stable release artifact.
4. Pin Python/Node base images by digest for stable releases. Pin or lock Python
   dependencies to a reproducible release input; retain the source dependency
   floors only where normal development requires them.
5. Keep build tools, source files, Node modules, package caches, and model files
   out of the final runtime layer.
6. Preserve the existing non-root user, `/data` volume, health check, and
   `IE_ALLOW_NETWORK_BIND=true` container configuration.
7. Ensure the final wheel contains the dashboard SPA. Verify both
   `engine/static/index.html` and its emitted asset directory are in the wheel
   before publishing an image.

### 3. Define the owner-facing deployment artifact

Add a release Compose template (for example `deploy/compose.yaml`) with these
mandatory properties:

- Image is supplied through an `IMAGE` variable and defaults only in documented
  examples; the release-specific rendered Compose file pins a `@sha256:` digest.
- Persistent named volume is mounted at `/data`.
- Host exposure defaults to `127.0.0.1:8000:8000`; public HTTPS is terminated by
  Caddy, nginx, Traefik, or a private network boundary.
- `IE_REQUIRE_AUTH=true`, `IE_REQUIRE_MODEL_READY=true`, and
  `IE_ALLOW_NETWORK_BIND=true` are set for network deployments.
- `stop_grace_period` remains longer than the configured drain timeout.
- Secrets are loaded from an uncommitted environment/secret source; no API key,
  remote-provider credential, or webhook signing secret is committed.
- The template documents a host mount or dashboard-based workflow for GGUF
  models, without copying models into the release image.

The documented first-use flow must be exactly achievable:

```bash
export IMAGE="ghcr.io/<owner>/inference-engine@sha256:<published-digest>"
docker compose -f deploy/compose.yaml pull
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml exec inference-engine \
  inference-engine keys create --label owner
```

The final command prints the initial operator key once. Document that the owner
must save it before opening `/dashboard`.

### 4. Build the release workflow

Create a dedicated GitHub Actions workflow triggered by a protected version tag
and manually via `workflow_dispatch` for dry runs. It must not publish from an
ordinary push or pull request.

Use this order, with each stage failing closed:

1. Check out the exact tag at full history depth as needed for provenance.
2. Validate the tag/version and ensure the working tree is clean.
3. Run the existing mandatory quality gates: formatting, linting, type checking,
   Python tests with coverage, and dashboard type checking/tests/build.
4. Run the existing real CPU llama.cpp/GGUF integration path on an AVX2 runner.
   Verify the model download checksum before loading it.
5. Build the OCI image once with BuildKit/buildx for `linux/amd64`; load or export
   that exact image locally for the following image tests. Do not rebuild later
   for publication.
6. Inspect the image:
   - image runs as the non-root engine user;
   - `inference-engine version` reports the tag commit and build date;
   - the installed package version agrees with the tag;
   - the CPU llama package is importable;
   - dashboard static files are present.
7. Run an image-level end-to-end test using a new persistent volume and the exact
   built image:
   - start the Compose service;
   - wait for `/healthz` and assert `200`;
   - verify `/readyz` is not ready before a required model is loaded;
   - assert `/dashboard` is served;
   - assert unauthenticated operator data/control endpoints return `401` over the
     network-facing path;
   - create an owner key using the container CLI;
   - import or mount the checksum-verified tiny GGUF fixture;
   - load the model and wait for `/readyz` to return `200`;
   - send an OpenAI-compatible streamed generation and verify ordered content and
     terminal completion;
   - verify a dashboard/operator API request works with the owner key;
   - restart the container against the same volume and verify the database and
     model registry persist;
   - stop the service and verify it drains cleanly within its grace period.
8. Generate the SBOM from the final image/package inputs and run the agreed
   vulnerability scan. Fail on critical vulnerabilities unless there is a
   documented, time-bounded exception in the release notes.
9. Create image provenance/attestation and sign it with GitHub OIDC. Do not store
   a long-lived registry signing key in repository secrets.
10. Push the **already tested** image digest to the approved registry.
11. Apply stable tags only after the digest push and attestation succeed.
12. Create the GitHub release and upload the release Compose file, SBOM, checksums,
    provenance reference, and release notes containing the immutable digest.

Use least-privilege workflow permissions. Do not print credentials, tokens,
registry login data, model URLs containing tokens, or the generated owner key in
workflow logs or release artifacts.

### 5. Enforce “build once, promote by digest”

The workflow must build exactly one candidate image for a release. Every test,
SBOM, attestation, image push, and stable tag must reference its same SHA-256
digest. It is forbidden to rebuild after tests merely to add tags or publish.

The release notes must include this exact immutable image form:

```text
ghcr.io/<owner>/inference-engine@sha256:<digest>
```

### 6. Document installation, upgrade, and rollback

Create or update the deployment documentation with concise owner instructions.

#### Initial installation

1. Verify an AVX2-capable Linux x86-64 host with Docker Engine and Docker Compose
   plugin.
2. Copy the release Compose file and a local environment file, then set the
   published digest.
3. Start the service with `docker compose pull` then `docker compose up -d`.
4. Create and securely retain the owner key through `docker compose exec`.
5. Place/import/download a GGUF model, load it in `/dashboard`, and verify
   `/readyz`.
6. Place the service behind TLS/reverse proxy before public exposure.

#### Upgrade

1. Back up `/data` using the documented backup command and separately back up
   `/data/models`.
2. Replace the image digest with the new release digest.
3. Pull and restart through Compose.
4. Verify `/version`, `/healthz`, `/readyz`, dashboard access, and a small
   generation.

#### Rollback

1. Stop the new image.
2. Pin the immediately previous known-good digest.
3. Restore the backup only when the documented migration compatibility boundary
   requires it.
4. Start, then verify health and a small generation.

Do not promise database rollback safety unless migrations have been explicitly
tested both forward and backward. Document any irreversible migration before the
release is published.

## Required acceptance checklist

A release is complete only when all items are true:

- [ ] A clean Linux x86-64 AVX2 host can deploy the published digest without
      building source code.
- [ ] The published image includes the CPU llama.cpp backend and bundled
      `/dashboard` assets.
- [ ] The exact published digest passed the full image-level real-GGUF test.
- [ ] The server runs non-root and persists all operator-critical state in `/data`.
- [ ] The dashboard is reachable at `/dashboard` and its data/control APIs reject
      unauthenticated network callers.
- [ ] The owner can create an initial key, load a model, generate text, and use the
      dashboard without installing Python or Node.
- [ ] The release carries an SBOM, vulnerability result, provenance/signature, and
      immutable digest.
- [ ] Stable tags were applied only to the tested digest.
- [ ] Installation, upgrade, and rollback documentation has been verified against
      a clean host/runner.
- [ ] Release notes state Linux x86-64 AVX2 CPU as the support boundary and make no
      untested GPU claim.

## Future GPU release line (do not implement in this CPU release)

When GPU support has real hardware validation, create separate artifacts such as
`vMAJOR.MINOR.PATCH-cuda12`; do not add CUDA/ROCm libraries to the CPU image.
Each GPU line needs its own base image, driver/runtime compatibility matrix,
target-hardware test runner, performance/health checks, SBOM, attestation, and
owner documentation. The CPU image remains the default small, predictable
deployment artifact.
