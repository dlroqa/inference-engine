# Releasing

How a stable release of the prebuilt CPU image is cut. Owners deploying a release
should read [deployment.md](deployment.md) instead.

## Release contract

| Decision | Value |
|---|---|
| Registry | `ghcr.io/dlroqa/inference-engine` (pushed by the workflow's `GITHUB_TOKEN`) |
| Release authority | A repository admin pushes a `vX.Y.Z` tag on `main`; `v*` tags are protected by a tag ruleset (only admins may create, move, or delete them) |
| Versioning | Semantic versioning; `engine/__init__.py` `__version__` is the single source (`pyproject.toml` reads it) |
| Changelog | `CHANGELOG.md`, Keep a Changelog format; every release needs a non-empty `## [X.Y.Z]` section |
| Platform | `linux/amd64`, CPU, Linux x86-64 host with AVX2. No macOS/Windows/ARM/GPU claim |

For every `vX.Y.Z` the workflow publishes: the image digest, tags `vX.Y.Z` / `X.Y` /
`X` / `latest` (applied only to the tested, attested digest), `sha-<commit>` for
traceability, and a GitHub release with the digest-pinned `compose.yaml`, the env
template, the SPDX SBOM, the grype report, Sigstore bundles for the provenance and
SBOM attestations, a provenance reference, the notes, and `SHA256SUMS`.

`X.Y`, `X`, and `latest` only move when the release is the highest version in that
line, so a patch to an older line never drags `latest` backwards.

## Cutting a release

1. **Prepare on a branch, merge via PR** (CI must be green):
   - bump `__version__` in `engine/__init__.py`;
   - move `[Unreleased]` entries under a new `## [X.Y.Z] - YYYY-MM-DD` heading in
     `CHANGELOG.md`, and call out any **new database migration** (it makes a
     rollback require a backup restore) under "Upgrade notes";
   - if runtime dependencies changed, regenerate the lock (below).
2. **Dry run** (optional but recommended): Actions → *Release* → *Run workflow*
   on `main`. Everything up to the vulnerability gate runs against the real image,
   and the step summary shows the rendered release notes. Nothing is pushed. Pull
   requests that touch release inputs (`Dockerfile`, `deploy/`, `requirements/`,
   the release scripts or workflow) run the same dry run automatically.
3. **Tag** the merge commit on `main` and push the tag:

   ```bash
   git switch main && git pull
   git tag -a vX.Y.Z -m "Inference Engine vX.Y.Z"
   git push origin vX.Y.Z
   ```

4. **Watch** `gh run watch <id> --exit-status`. A failure at any stage publishes
   nothing (or, after the digest push, nothing beyond `sha-<commit>`). Fix, bump
   the patch version, and tag again — never move a published tag.
5. **First release only:** GHCR creates the package as private. In the package
   settings (github.com/users/dlroqa/packages/container/inference-engine/settings)
   set visibility to **public** so owners can `docker compose pull` without a login,
   then check that an anonymous pull by digest works.

## What the workflow does

`.github/workflows/release.yml`, in order, each stage failing closed:

1. **validate** — tag is `vMAJOR.MINOR.PATCH`, equals `engine.__version__`, points at
   the checked-out commit, is on `main`; `CHANGELOG.md` has the section; clean tree.
   Build date is a UTC RFC 3339 timestamp.
2. **quality** — calls `ci.yml` verbatim: ruff format/lint, mypy, pytest + coverage,
   dashboard typecheck/tests/build, wheel build + dashboard-in-wheel check, and the
   real llama.cpp GGUF integration job on an AVX2 runner (model checksum verified).
3. **candidate** (read-only token) — **one** `docker buildx build` for
   `linux/amd64` exporting the same result as an OCI archive (what gets pushed) and
   a daemon image (what gets tested); proves the tested image's config digest equals
   the archive's. Then: image inspection (non-root, version/commit/date, package
   version, `llama_cpp` import, dashboard files, no toolchain, OCI labels); the
   image-level E2E (`scripts/release_image_e2e.py`) through `deploy/compose.yaml`
   on a fresh volume; SPDX SBOM (syft); grype scan + gate.
4. **publish** (tags only; `packages`/`attestations`/`id-token`/`contents` write)
   — re-verifies the archive checksum and digest, pushes it with
   `skopeo copy --preserve-digests` as `sha-<commit>` and checks the registry digest,
   creates SLSA provenance and SBOM attestations signed through GitHub OIDC
   (Sigstore; no stored signing key), applies stable tags by digest, then creates
   the GitHub release.

Nothing is rebuilt after testing: every later step references the same digest.

## Inputs that are locked

- **Base images** — `node:22-slim` and `python:3.12-slim` pinned by `@sha256:` in
  the `Dockerfile`. To bump, resolve the new index digest and update both `FROM`
  lines for Python together.
- **Runtime Python dependencies** — `requirements/release-cpu.txt`, hash-locked,
  including the `llama-cpp-python` CPU wheel (the `manylinux2014` build from the
  CPU wheel index; the index's plain `linux_x86_64` wheels for some versions link
  musl and do not load on Debian). Regenerate with:

  ```bash
  uv pip compile pyproject.toml --extra llama --python-version 3.12 \
    --python-platform x86_64-manylinux_2_28 --generate-hashes \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu \
    --index-strategy unsafe-best-match --emit-index-url --no-header \
    -o requirements/release-cpu.txt
  ```

  The image build installs only from this file (`--require-hashes
  --only-binary=:all:`) and runs `pip check`. The `pyproject.toml` floors stay for
  development installs.
- **Workflow actions** — pinned by commit SHA in `release.yml`.

## Vulnerability gate

The candidate is scanned with grype from its SBOM. Any **critical** finding fails
the release unless `deploy/vulnerability-exceptions.json` lists it with `id`,
`package`, a `reason`, and an `expires` date. Expired or incomplete entries fail
the release too, and every applied exception is printed in the release notes.
Keep exceptions short-lived; prefer rebuilding on a patched base image.

## Verifying a published release

```bash
gh attestation verify oci://ghcr.io/dlroqa/inference-engine@sha256:<digest> \
  --repo dlroqa/inference-engine
docker buildx imagetools inspect ghcr.io/dlroqa/inference-engine:vX.Y.Z   # same digest
```

## Future GPU lines

GPU images will be separate artifacts (e.g. `vX.Y.Z-cuda12`) with their own base
image, hardware-qualified test runner, SBOM, and attestations. CUDA/ROCm libraries
never go into the CPU image.
