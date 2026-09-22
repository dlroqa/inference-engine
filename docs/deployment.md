# Production deployment (Block 9)

A supported deployment is **reproducible, observable, upgradeable, and
recoverable**. This document covers the Docker path, graceful shutdown, readiness,
backup/restore, the supported-platform matrix, and supply-chain basics. Security
hardening (network policy, TLS guidance, kill switches, audit) is in
[docs/security.md](security.md).

## Docker (supported path)

```bash
docker build -t inference-engine:latest .
docker run --rm -p 127.0.0.1:8000:8000 -v engine-data:/data inference-engine:latest
```

Or with Compose:

```bash
docker compose up --build
```

The image is CPU-only and runs as a **non-root** user. The llama.cpp backend is
optional (it requires an **AVX2** host); include it with:

```bash
docker build --build-arg INSTALL_LLAMA=true -t inference-engine:latest .
```

Record the build provenance so `/version` and `/healthz` report it:

```bash
docker build \
  --build-arg IE_BUILD_SHA="$(git rev-parse --short HEAD)" \
  --build-arg IE_BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t inference-engine:latest .
```

### Volumes, model store, and database

Everything operator-critical lives under **`/data`** (`IE_DATA_DIR`), which must be
a persistent volume:

| Path | Contents |
|---|---|
| `/data/inference_engine.db` | SQLite (registry, API keys, usage, logs) |
| `/data/models/` | Imported/downloaded GGUF model files |
| `/data/logs/` | Structured log files |

The database and models have different backup needs — see **Backup & restore**.

### Reverse proxy / TLS

The engine does **not** terminate TLS. Put a reverse proxy (nginx, Caddy,
Traefik) in front to terminate TLS and forward to the container. Inside the
container the app binds `0.0.0.0:8000` with `IE_ALLOW_NETWORK_BIND=true`; on a
non-loopback bind **auth is required by default** (create a key with
`inference-engine keys create`). Example Caddy config:

```
inference.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

## Graceful shutdown & drain

On `SIGTERM` (e.g. `docker stop`, a rolling update) the engine **drains**: it stops
admitting new work (the scheduler refuses new requests and `/readyz` reports
not-ready so a balancer stops routing), lets in-flight generations finish for up to
`drain_timeout_s` (default 30s), then releases the model and exits. Give the
container enough stop grace: Compose sets `stop_grace_period: 40s`.

## Readiness & liveness

| Endpoint | Meaning |
|---|---|
| `GET /healthz` | Liveness — the process is up (also returns version + build). |
| `GET /readyz` | Readiness — DB reachable + migrations applied; `503` while draining. |
| `GET /version` | Version, commit, and build date. |

Set `require_model_ready=true` (`IE_REQUIRE_MODEL_READY`) so `/readyz` is not-ready
until a model is loaded — use this to gate a load balancer behind actual inference
capacity. Point liveness probes at `/healthz` and readiness/routing probes at
`/readyz`.

## Backup & restore

The database and the active config back up into one checksum-verified tarball.
**Model files are excluded** (large, content-addressed, re-downloadable) — back up
`/data/models/` separately with your normal file backups; the registry records
each model's SHA-256 for re-verification.

```bash
# Back up (writes inference-engine-backup-<UTC>.tar.gz into the target dir)
inference-engine backup --out /backups

# Restore into a fresh install (refuses to overwrite an existing DB without --force)
inference-engine restore --from /backups/inference-engine-backup-*.tar.gz
```

Restore verifies the manifest checksums before writing, so a corrupt archive fails
loudly rather than installing bad state. No manual SQL is ever required.

## Upgrades

1. Back up (`inference-engine backup`).
2. Pull/build the new image.
3. Start it against the **same** `/data` volume — migrations apply automatically on
   startup (ordered, idempotent, tracked in `schema_migrations`).
4. Verify `/version` and `/readyz`. Roll back by redeploying the previous image
   against a restored backup if needed.

## Supported-platform matrix

| Platform | Status |
|---|---|
| Linux x86-64, CPU | ✅ Supported — tested in CI (incl. the real-model AVX2 job) |
| Docker (Linux host) | ✅ Supported — image build + start smoke-tested in CI |
| macOS (Apple Silicon / Intel), CPU | ⚠️ Best-effort — runs, not yet continuously tested |
| Windows x86-64, CPU | ⚠️ Best-effort — runs, not yet continuously tested |
| Any GPU backend | ❌ Not claimed — no GPU is tested on real hardware yet |

"Supported" means continuously tested in CI. Best-effort platforms are expected to
work but are not gated on.

## Supply-chain basics

- **Version metadata:** `/version` and `/healthz` report the package version and
  the build commit/date injected at image build time.
- **Model integrity:** every registry model records a SHA-256, verified on
  import/download and re-checkable after restore.
- **Dependency inventory / SBOM:** generate a CycloneDX component list for a
  release with `python scripts/generate_sbom.py --out sbom.json`.
- **Lock policy:** runtime dependencies are version-floored in `pyproject.toml`;
  for a byte-reproducible deploy, pin a full lock with `pip freeze > requirements.lock`
  from the built image and archive it with the release. CVE scanning is covered in
  [docs/security.md](security.md).
