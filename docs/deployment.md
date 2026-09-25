# Production deployment

A supported deployment is **reproducible, observable, upgradeable, and
recoverable**. The supported path is the **prebuilt CPU release image**: a server
owner deploys a published digest and never builds Python, Node, the dashboard, or
`llama-cpp-python`. Security hardening (network policy, TLS guidance, kill
switches, audit) is in [docs/security.md](security.md); how releases are cut is in
[docs/releasing.md](releasing.md).

## Support boundary

| Platform | Status |
|---|---|
| Linux x86-64 with **AVX2**, Docker Engine + Compose plugin ≥ 2.24 | ✅ Supported — the release image is tested end to end on this in CI |
| Build from source (pip / `docker compose up --build`) | 🛠️ Development path — not the supported server deployment |
| macOS, Windows, ARM | ❌ Not supported by the release image |
| Any GPU backend (CUDA/ROCm) | ❌ Not claimed — no GPU is tested on real hardware yet |

Check AVX2 with `grep -m1 -o avx2 /proc/cpuinfo`. Without it, llama.cpp crashes
with "Illegal instruction" when a model loads.

## What a release contains

Every stable release `vX.Y.Z` (see its GitHub release page) publishes:

| Artifact | Use |
|---|---|
| `ghcr.io/dlroqa/inference-engine@sha256:<digest>` | The immutable image. **Deploy by digest.** |
| Tags `vX.Y.Z`, `X.Y`, `X`, `latest` | Convenience pointers to a tested digest — do not deploy by tag |
| `compose.yaml` | `deploy/compose.yaml` rendered with the release digest |
| `inference-engine.env.example` | Template for your local, uncommitted settings/secrets file |
| `sbom.spdx.json`, `SHA256SUMS`, attestations | Supply-chain verification |

The image contains the engine, migrations, the llama.cpp CPU backend, and the
dashboard at `/dashboard`. It contains **no models**.

## Initial installation

1. **Host:** Linux x86-64 with AVX2, Docker Engine, and the Docker Compose plugin.
2. **Files:** download `compose.yaml` and `inference-engine.env.example` from the
   release, then create your local settings file (never commit it):

   ```bash
   cp inference-engine.env.example inference-engine.env
   ```

   Alternatively, use `deploy/compose.yaml` from a checkout and supply the digest
   from the release notes:

   ```bash
   export IMAGE="ghcr.io/dlroqa/inference-engine@sha256:<published-digest>"
   ```

3. **Start:**

   ```bash
   docker compose -f deploy/compose.yaml pull
   docker compose -f deploy/compose.yaml up -d
   ```

4. **Create the owner key** and save it somewhere safe — it is printed **once**:

   ```bash
   docker compose -f deploy/compose.yaml exec inference-engine \
     inference-engine keys create --label owner
   ```

5. **Add a model:** open `http://127.0.0.1:8000/dashboard`, enter the owner key,
   and on **Models** download a GGUF (Hugging Face repo + file, or URL, with its
   SHA-256) or import one from a path under `/data/models`. To use models you
   already have on the host, uncomment the `/srv/models:/data/models` mount in
   the Compose file. Load the model, then confirm readiness:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/readyz   # 200
   ```

6. **Before public exposure,** put the service behind TLS (below) or a private
   network boundary. The Compose file publishes only on `127.0.0.1:8000`.

(The examples use `-f deploy/compose.yaml`; with the release's `compose.yaml`
use `-f compose.yaml`.)

### What the Compose file enforces

- Image pinned by `@sha256:` digest (the release copy) or required via `IMAGE`.
- Named volume `engine-data` at `/data` for everything operator-critical.
- `127.0.0.1:8000:8000` host exposure only.
- `IE_REQUIRE_AUTH=true`, `IE_REQUIRE_MODEL_READY=true`, `IE_ALLOW_NETWORK_BIND=true`.
- `stop_grace_period: 40s`, longer than the 30s drain timeout.
- Secrets from the uncommitted `inference-engine.env`; nothing secret is committed.

### Volumes, model store, and database

| Path | Contents |
|---|---|
| `/data/inference_engine.db` | SQLite (registry, API keys, usage, logs, audit) |
| `/data/models/` | Imported/downloaded GGUF model files |
| `/data/logs/` | Structured log files |

### Reverse proxy / TLS

The engine does **not** terminate TLS. Put a reverse proxy in front. Inside the
container the app binds `0.0.0.0:8000`; network callers always need an API key.
Example Caddy config:

```
inference.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

## Upgrade

1. **Back up** the database/config, then the models separately (model files are
   excluded from backups):

   ```bash
   docker compose -f deploy/compose.yaml exec inference-engine \
     inference-engine backup --out /data/backups
   docker compose -f deploy/compose.yaml cp inference-engine:/data/backups ./backups
   docker compose -f deploy/compose.yaml cp inference-engine:/data/models ./models-backup
   ```

2. **Pin the new digest:** use the new release's `compose.yaml`, or
   `export IMAGE=ghcr.io/dlroqa/inference-engine@sha256:<new-digest>`.
3. **Pull and restart:** `docker compose -f deploy/compose.yaml pull && docker compose -f deploy/compose.yaml up -d`.
   Migrations apply automatically on startup.
4. **Verify:** `/version` reports the new version, `/healthz` is `200`, load the
   model and check `/readyz` is `200`, open `/dashboard`, and send a small
   generation.

## Rollback

1. **Stop** the new image: `docker compose -f deploy/compose.yaml stop`.
2. **Pin** the immediately previous known-good digest (from that release's notes).
3. **Restore only if required.** Migrations are forward-only and are **not**
   tested backward. If the new release's notes list a new migration, restore the
   pre-upgrade backup with the **old** image before starting it:

   ```bash
   docker compose -f deploy/compose.yaml run --rm -v "$PWD/backups:/backups:ro" \
     inference-engine inference-engine restore --from /backups/<archive>.tar.gz --force
   ```

4. **Start and verify:** `docker compose -f deploy/compose.yaml up -d`, then
   `/healthz`, model load, and a small generation.

## Graceful shutdown & drain

On `SIGTERM` (`docker compose stop`, a rolling update) the engine **drains**: it
stops admitting new work (`/readyz` reports not-ready), lets in-flight generations
finish for up to `drain_timeout_s` (default 30s), then releases the model and exits.
The Compose `stop_grace_period` (40s) must stay longer than the drain timeout.

## Readiness & liveness

| Endpoint | Meaning |
|---|---|
| `GET /healthz` | Liveness — the process is up (also returns version + build). |
| `GET /readyz` | Readiness — DB + migrations; `503` while draining or (with `IE_REQUIRE_MODEL_READY`) until a model is loaded. |
| `GET /version` | Version, commit, and build date. |

## Backup & restore

The database and the active config back up into one checksum-verified tarball.
**Model files are excluded** — back up `/data/models/` separately; the registry
records each model's SHA-256 for re-verification. Restore verifies the manifest
checksums before writing and refuses to overwrite an existing database without
`--force`. No manual SQL is ever required.

## Building from source (development)

```bash
docker compose up --build          # root docker-compose.yml builds the same Dockerfile
```

The Dockerfile always installs the hash-locked llama.cpp CPU backend. Pass
`--build-arg IE_BUILD_SHA=... --build-arg IE_BUILD_DATE=... --build-arg IE_VERSION=...`
to record provenance in `/version` and the OCI labels.

## Supply chain

- **Immutable digests:** releases are built once, tested, and promoted by digest.
- **Provenance + SBOM:** signed with GitHub Actions OIDC (Sigstore) and pushed with
  the image. Verify with
  `gh attestation verify oci://ghcr.io/dlroqa/inference-engine@sha256:<digest> --repo dlroqa/inference-engine`.
- **Locked inputs:** base images pinned by digest; runtime Python dependencies
  hash-locked in `requirements/release-cpu.txt`.
- **Vulnerability gate:** each release is scanned with grype and fails on critical
  findings unless a documented, time-bounded exception is listed in its notes.
- **Model integrity:** every registry model records a SHA-256, verified on
  import/download.
