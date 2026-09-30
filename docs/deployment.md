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
| `install.sh` | One-command Linux installer; verifies downloaded deployment assets against `SHA256SUMS` |
| `inference-engine.env.example` | Template for your local, uncommitted settings/secrets file |
| `sbom.spdx.json`, `SHA256SUMS`, attestations | Supply-chain verification |

The image contains the engine, migrations, the llama.cpp CPU backend, and the
dashboard at `/dashboard`. It contains **no models**.

## Initial installation

1. **Host:** Linux x86-64 with AVX2, Docker Engine, and the Docker Compose plugin.
2. **One-command install:** for a published `vX.Y.Z`, run the release installer:

   ```bash
   curl --fail --location --proto '=https' --tlsv1.2 \
     https://github.com/dlroqa/inference-engine/releases/download/vX.Y.Z/install.sh \
     | bash -s -- vX.Y.Z
   ```

   It verifies the downloaded Compose file and settings template against the
   release's `SHA256SUMS`, then pulls and starts the digest-pinned image. Set
   `IE_INSTALL_DIR=/srv/inference-engine` before the command to choose the
   deployment directory. The initial owner-key command is printed after startup.

   For a separately verified bootstrap, download `install.sh` and `SHA256SUMS`, run
   `sha256sum --strict --check --ignore-missing SHA256SUMS`, and then run
   `bash install.sh vX.Y.Z`.

3. **Manual files:** download `compose.yaml` and `inference-engine.env.example`
   from the release, then create your local settings file (never commit it):

   ```bash
   cp inference-engine.env.example inference-engine.env
   ```

   Alternatively, use `deploy/compose.yaml` from a checkout and supply the digest
   from the release notes:

   ```bash
   export IMAGE="ghcr.io/dlroqa/inference-engine@sha256:<published-digest>"
   ```

4. **Start:**

   ```bash
   docker compose -f deploy/compose.yaml pull
   docker compose -f deploy/compose.yaml up -d
   ```

5. **Create the owner key** and save it somewhere safe — it is printed **once**:

   ```bash
   docker compose -f deploy/compose.yaml exec inference-engine \
     inference-engine keys create --label owner
   ```

6. **Add a model:** open `http://127.0.0.1:8000/dashboard`, enter the owner key,
   and on **Models** download a GGUF (Hugging Face repo + file, or URL, with its
   SHA-256) or import one from a path under `/data/models`. To use models you
   already have on the host, uncomment the `/srv/models:/data/models` mount in
   the Compose file. Load the model, then confirm readiness:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/readyz   # 200
   ```

7. **Before public exposure,** put the service behind TLS (below) or a private
   network boundary. The Compose file publishes only on `127.0.0.1:8000`.

(The examples use `-f deploy/compose.yaml`; with the release's `compose.yaml`
use `-f compose.yaml`.)

### What the Compose file enforces

- Image pinned by `@sha256:` digest (the release copy) or required via `IMAGE`.
- Named volume `engine-data` at `/data` for everything operator-critical.
- `127.0.0.1:8000:8000` host exposure only.
- `IE_REQUIRE_AUTH=true`, `IE_REQUIRE_MODEL_READY=true`, `IE_ALLOW_NETWORK_BIND=true`.
- `stop_grace_period: 40s`, longer than the 30s drain timeout. This is the
  supervisor's allowance before it kills the process, not a shutdown guarantee
  (see [Graceful shutdown & drain](#graceful-shutdown--drain)).
- Secrets from the uncommitted `inference-engine.env`; nothing secret is committed.
- Least privilege: `security_opt: ["no-new-privileges:true"]` and `cap_drop: [ALL]`
  (see [Container privileges](#container-privileges)).

### Container privileges

The engine runs as the image's non-root user `engine` (uid 10001), listens on
port 8000 inside the container and needs **no Linux capabilities**. The Compose
file therefore:

- drops every capability (`cap_drop: [ALL]`), including the bounding set, so no
  process in the container can hold or regain one;
- sets `no-new-privileges`, so the setuid/setgid programs that the Debian base
  ships (for example `su`, `mount`, `passwd`) cannot raise privileges, even after
  code execution as uid 10001.

`docker compose exec` and `docker compose run` (backups, key creation, restore)
use the same settings. Each Release run records the image's setuid/setgid and
file-capability files and the engine user's capability sets (with Docker's
defaults and with these settings) in its summary. The image end-to-end test
asserts the settings on the running engine (`CapEff`/`CapPrm`/`CapBnd`/`CapAmb`
all zero, `NoNewPrivs: 1`) and runs install, model import and load,
generation, restart, drain, backup, database loss and restore under them. The
store-ownership and interrupted-download recovery test runs its engines with
them too.

Keep these settings when you adapt the file:

- Do not add `cap_add`, `privileged`, or `user: root`. Nothing in the engine
  needs them; a permission error means the volume ownership is wrong.
- Files the engine writes must belong to uid/gid 10001. A fresh named volume
  already does, because Docker copies the image's `/data` (owned by `engine`)
  into it. A host directory you mount (for example `/srv/models:/data/models`)
  must be writable by uid 10001 (`sudo chown -R 10001:10001 /srv/models`), since
  without `CAP_DAC_OVERRIDE` nothing in the container can bypass file permissions.
- To prepare or repair a volume's ownership, use a separate one-off container
  (`docker run --rm --user root -v <volume>:/v <image> chown -R engine:engine /v`),
  not the service.

The development `docker-compose.yml` at the repository root does not set these
options; the release Compose file is the supported deployment.

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

### Upgrading to a release that includes `1f3bbe0` (A3b) or later

- **Webhook endpoints with non-ASCII hosts stop receiving deliveries.** Such an
  endpoint (registered before hosts had to be ASCII) is never sent to: each
  delivery is dead-lettered at once with "endpoint host is not ASCII; re-register
  it in the xn-- (punycode) form". Stored endpoints are not rewritten. Before or
  right after upgrading, list endpoints (`GET /admin/billing/webhooks/endpoints`,
  or the dashboard's Clients view) and, for each non-ASCII host: register the
  `xn--` form of the same URL, configure the receiver with the **new** signing
  secret shown once at registration, then delete the old endpoint. Dead
  deliveries do **not** move to the new endpoint (a replay re-sends to the
  delivery's own endpoint). See [Webhooks](webhooks.md#security-notes).
- Webhook deliveries no longer follow redirects: a receiver that answers 3xx
  now fails the attempt. Point endpoints at their final URL.

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

**There is no fixed shutdown duration.** Shutdown requests cooperative stopping
and waits for the current webhook delivery, each download worker, and each gRPC
request's final usage write to finish, along with their persistence work.
Network timeouts limit individual blocking operations; they do not guarantee a
maximum shutdown duration. Several operations, redirects, database and
filesystem work, and the sequential cleanup stages (gRPC stop, request drain,
webhook worker, remote workers, telemetry, downloads, model unload) all add to
the elapsed time.

**Supervisor grace period.** The Compose `stop_grace_period` (40s) is how long
the supervisor waits before killing the process. Keep it longer than
`drain_timeout_s`, but that alone does not budget for gRPC, webhook, download,
persistence and backend cleanup, and a longer grace period is still not a hard
guarantee. Choose it for your workload. A forced kill can interrupt in-flight
work; recovery at the next start then applies (see below).

In-flight model downloads are stopped during shutdown too. Shutdown requests
cancellation of each download and waits until its worker has actually stopped.
While the process is allowed to finish, no worker changes model files after
shutdown returns. A worker usually stops at its next chunk.

There is no fixed upper bound on this wait:

- The 10s download grace period is only a warning threshold. After it, the engine
  logs `model_download_shutdown_waiting` and keeps waiting.
- The 30s network timeout applies to each blocking read, not to the whole
  shutdown.
- Checksumming a large file stops between chunks.

**Forced termination.** Compose, Kubernetes or systemd may kill the process
before shutdown finishes, for example when `stop_grace_period` elapses. No
engine guarantee holds past a forced kill. What remains depends on how far the
download had got:

- **Before the final rename:** a `.part` file may remain. If it does and is
  intact, a later download of the same file can resume from it under the normal
  resume and checksum rules. It is not guaranteed to exist, or to be intact,
  after every crash.
- **After the final rename:** the model file may already be in place while its
  metadata probe or its registry update was unfinished. There is then no
  `.part` file.

Either way, the killed process's registry row still says `downloading`. On the
next start, before admitting any model operation, the engine marks every such
row `error` with a fixed message: "download interrupted: the engine stopped
before it finished. Delete this model (which removes any file it kept) and
download it again." It logs `model_download_interrupted` with a fixed label for
the files it found (`partial`, `promoted`, `both` or `neither`). Recovery is
deliberately conservative:

- Kept files are not changed or deleted, and links are not followed.
- A file is never trusted: nothing is marked ready or loaded, and there is no
  automatic resume.
- The update is conditional, so later starts change nothing, and rows that had
  already finished are never touched.
- If the database update fails, startup fails rather than claiming recovery.

To recover, delete the model (dashboard Delete or `DELETE /admin/models/{id}`,
which needs model management enabled). This removes the engine-managed file
and `.part` file it kept. Then download it again.

**One engine per writable store.** A serving engine (which can serve any
number of clients) exclusively owns two things: its **database** and its
**managed model directory** (`models_dir`, `/data/models` by default). Recovery
and model operations are only correct if no other engine writes either one. For
its whole lifetime the engine holds two exclusive, non-blocking OS locks:

- `<database>.lock`, next to the database (`inference_engine.db.lock` by
  default);
- `.engine-model-store.lock`, inside the model directory. Engines that reach the
  same directory through different paths or container mount points contend on
  this same file.

Both paths are resolved to canonical absolute paths first (relative paths,
`..` and symlinked directories). The database lock is taken first and released
last. A second engine that shares either resource refuses to start, with
`StoreLockedError` naming the resource: "another engine process already owns
this database" or "... this model directory". It writes nothing to that store,
not even log rows. A lock file that cannot be opened or locked (permissions, a
filesystem without lock support) fails startup with
`StoreLockUnavailableError` instead. The engine user needs write access to
both lock files.

| Arrangement | Supported |
| --- | --- |
| One engine serving many clients | Yes: one exclusively owned store |
| Several engines, each with its own database and model directory | Yes |
| Several engines sharing a database or a writable model directory | No: the second refuses to start |
| Shared read-only model assets | Not in this version (see `docs/rfcs/shared-read-only-model-assets.md`) |

Locks and shutdown:

- The OS releases both locks when the process exits, however it exits
  (including a forced kill). The lock files stay behind, unlocked. That is
  normal: they are reused, never cleaned up, and do not mean another engine is
  running.
- Do not delete, move or replace a lock file to get around a refusal. Stop the
  other engine instead.
- Shutdown releases the locks only after the **whole** cleanup sequence has run
  and every known store writer has stopped: download workers (including their
  threads), the webhook delivery worker together with its in-flight batch
  thread, and the gRPC server together with every request handler. The webhook
  batch finishes the delivery it is on and records its outcome, and leaves the
  rest pending, unsent. `webhook_delivery_timeout_s` limits each network
  operation of that delivery, not the wait.
  - **Cancellation.** If the shutdown is cancelled, even repeatedly, cleanup is
    not cut short: it runs to the end, the locks are released, and then the
    cancellation is passed on.
  - **Failures.** If a cleanup step fails, the later steps still run and the
    first failure is reported. When cancellation and a failure coincide, the
    cancellation wins and the failure is logged
    (`shutdown_failed_while_cancelled`).
  - **Cut-short cleanup.** If the cleanup task itself is cancelled from
    inside (not by the caller), shutdown logs `shutdown_incomplete` and raises
    `ShutdownIncompleteError` rather than reporting success. If the caller was
    also cancelled, the cancellation wins and the diagnostic is still logged.
  - **Unproven writers.** If a writer cannot be proven stopped (a gRPC stop
    failed or was interrupted, or the cleanup was cut short), the engine keeps
    both locks and logs `store_ownership_retained` with fixed writer names
    (`grpc`, `webhook_task`, `webhook_thread`, `downloads`, `cleanup`) until the
    process exits.
- gRPC: calls get a 5-second grace period and are then cancelled. The gRPC
  stop counts as done only after the server has stopped **and** every request
  handler has ended, including its finalization; a handler left suspended by a
  cancelled call is finalized first. The grace period expiring, or
  cancellation being issued, is not treated as proof.
- Each gRPC request is finalized exactly once (capacity released, counters,
  route metrics and its usage record), also when a cancellation interrupts the
  closing of its stream; the cancellation then still propagates. Finalization
  runs once, but a database failure can still prevent the usage record from
  being written: that failure is logged (`grpc_request_finalize_failed`, by
  type) and never retried, since retrying could count the request twice.
- Covered writers are the ones the engine starts itself. HTTP requests have
  already finished when the server runs shutdown. A generation thread that is
  still running does not write the store. A supervisor that kills the process
  ends all of this; recovery at the next start then applies.
- **Retained ownership: what to do.** Retention protects a writer that may
  still be running. Read the shutdown diagnostics (`store_ownership_retained`,
  `shutdown_step_failed`, `shutdown_incomplete`), then stop the owning process
  (it exits, or your supervisor stops it) before starting an engine against
  its database or model directory. The OS releases the locks when that process
  exits. Do not delete, move or replace lock files, force-unlock them, or start
  a competing engine as a way to recover.
- Webhook delivery is at least once, not exactly once. A kill can land after a
  receiver accepted a delivery but before its success was recorded; restart
  recovery does not undo external effects, and that delivery is sent again.
  Receivers should deduplicate on the stable `webhook-id` header (see
  [webhooks](webhooks.md)).

Requirements and limits:

- Use distinct, non-overlapping stores. Do not nest one store inside another,
  do not import files across stores, and do not create hard-link aliases of a
  database.
- Keep the directory configuration trusted and stable. An administrator
  replacing lock files, or hostile path changes, are not defended against.
- The locks are qualified on local filesystems and Docker named volumes: the
  portable tests on GitHub-hosted Linux, macOS and Windows runners (one
  architecture each, as the CI runner inventory reports), and the image test
  on Linux x86-64. Other native targets are not qualified by this. Exclusion
  across hosts on NFS, SMB, FUSE or object-store mounts is not promised.
- The locks are advisory. They coordinate engines of this version, not external
  tools or older releases that do not take the model-directory lock. Stop every
  old engine before rolling out this version.
- Run offline maintenance with all engines stopped. Maintenance commands that
  open the database directly, such as backups, do not take these locks.

Scale out with separate stores (or remote workers), not by sharing one.

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
