# Security hardening (Block 9b)

The hardening baseline for an exposed or production deployment. Every control
defaults to the current permissive-but-safe behavior so existing setups are
unaffected; tighten them per your threat model. Deployment mechanics (Docker,
drain, backup) are in [docs/deployment.md](deployment.md).

## Network exposure & TLS

- **Loopback by default.** The server binds `127.0.0.1` unless you set
  `allow_network_bind=true`. On a non-loopback bind, **authentication is required**
  by default (`require_auth`); disabling it needs an explicit
  `allow_insecure_bind=true`.
- **Terminate TLS at a reverse proxy.** The engine does not serve TLS. Put nginx,
  Caddy, or Traefik in front, terminate TLS there, and forward to the app on a
  private interface. See the proxy example in [docs/deployment.md](deployment.md).

## Operator access

Operator surfaces (`/admin/*`, `/metrics`, `/logs`, `/diagnostics`, `/ws/*`)
accept only **operator** keys. A key created for a billing client is a *client*
key: it can call inference and `/client/*`, but operator surfaces answer
`403 operator_role_required` — from localhost too. Credentials are evaluated
before the loopback development exception, which applies only to keyless callers
while effective auth is off. `GET /admin/identity` reports the caller's key id,
safe prefix, label, and role (never the token). Full precedence table:
[docs/operability.md](operability.md#access-control).

## Trusted-network / IP policy

`ip_allowlist` (a list of CIDRs) restricts which client addresses may reach **any**
endpoint — non-matching clients get `403 ip_not_allowed` before any other work.

```toml
ip_allowlist = ["10.0.0.0/8", "192.168.1.0/24"]
```

By default the **direct peer IP** is checked. If the app sits behind a trusted
reverse proxy that sets `X-Forwarded-For`, set `trust_forwarded_for=true` to check
the forwarded client IP instead. **Only enable this behind a proxy you control** —
otherwise a client can spoof the header to bypass the allowlist.

## Egress policy

`allow_network_downloads=false` blocks the engine from fetching models over the
network (Hugging Face / direct URL) — for air-gapped installs. Local imports and
serving are unaffected; network download attempts return `403 downloads_disabled`.

## Kill switches

Disable dynamic or risky features at runtime without a code change:

| Setting | Default | Off ⇒ |
|---|---|---|
| `allow_model_management` | `true` | model import/download/load/unload/delete return `403 model_management_disabled` |
| `allow_network_downloads` | `true` | network downloads return `403 downloads_disabled` |
| `allow_structured_output` | `true` | JSON/grammar-constrained requests return `400 structured_output_disabled` |
| `diagnostics_enabled` | `true` | `GET /diagnostics` returns `403 diagnostics_disabled` |

## Safe-format enforcement

- **Request bodies** are size-capped (`max_request_bytes`, `413` when exceeded).
- **Dialect requests** are strictly validated: unknown/benign fields are ignored,
  but contract-changing features we cannot honor (tool calling, `n>1`, unknown
  `response_format` types, non-text Anthropic content) are **rejected** with a clear
  error rather than silently mishandled. See
  [docs/compatibility.md](compatibility.md).

## Log redaction

Logs and telemetry carry **structured metadata only** — request ids, routes, model
ids, key ids, token counts, timings, and error categories. **Prompts, responses,
and API-key secrets are never logged**, and the `/diagnostics` bundle passes config
through a redactor. Audit `detail` fields are non-secret metadata only. Turn the
diagnostics bundle off entirely with `diagnostics_enabled=false`.

**Model download failures.** A failed download's error text can quote the
download URL, a redirect target, or a request target. Before the
`model_download_failed` warning is logged, its `detail` is passed through the same
redaction the model API applies to responses: URL userinfo is removed, and query
values whose names look credential-bearing (`token`, `key`, `secret`, `sig`,
`auth`, `password`, `credential`, after one URL decode) are masked, including
inside a redirect URL passed as a query value. The redaction happens before the
logger is called, so every handler sees only the redacted text; the JSON
formatter's own query masking stays in place as a second layer. If the text cannot
be redacted, a fixed "error details withheld" message is logged instead. The log
line never carries the exception or its traceback.

Every ordinary failure of the background download task ends on that same path,
not only the downloader's own download errors: an invalid URL (rejected with a
fixed `invalid model URL (<type>)` message that does not quote it), a transport
error while reading, an unexpected exception in the worker, or a failure while
probing the finished file. The model is marked `error`, the warning names the
stage (`download` or `finalize`) and the exception type, and the exception never
escapes the task, so asyncio's "Task exception was never retrieved" report
cannot print it. Cancellation is not a failure: a cancelled download is marked
`cancelled` and logs `model_download_cancelled`.

**Download cancellation and worker lifetime.** A download runs in a worker
thread, and Python cannot force a thread to stop, so these steps are separate:

1. *Cancellation requested.* A cancel request (the API, task cancellation, or
   shutdown) sets the download's cooperative cancel flag at once.
2. *Worker stopped.* The worker checks the flag before starting and again after
   every network read, whatever the read returned. A cancel that arrives during
   a blocked read therefore discards the bytes or end-of-file it returns. It also
   checks between checksum chunks and at the final rename. When it stops, its
   response and file are closed, and the `.part` file is kept as a valid,
   resumable prefix.
3. *Outcome recorded.* Only after the worker has stopped does the engine record
   `cancelled` (or the worker's real outcome, if it failed or completed first)
   and log the event.
4. *Ownership released.* The engine keeps tracking the download until then, even
   if the task awaiting it was cancelled. Task cancellation is re-raised to its
   caller only after the worker has stopped.

The final rename of `.part` to the model file is the commit point. A cancel
request accepted before it prevents the rename. Once the file is renamed, the
download has committed: later cancel requests are refused, and it finishes as
`ready`, or `error` if post-download checks fail. The check and the rename share
a lock that is held only for the rename, never during network reads.

The cancel endpoint reports the engine's actual decision. An accepted request
answers `{"cancelling": true, "id": ...}`. That means the request was accepted,
not that the worker has already stopped; the model shows `cancelled` once it
has. A refused request answers `409 model_cancel_not_accepted`. This happens
once the file is committed and the download is finishing, or when no worker is
running for the model (for example after a forced kill). It never answers as if
a refused request had been accepted.

On shutdown, the engine requests cancellation of every download and waits until
each worker has stopped and its outcome is recorded. The 10-second grace period
is a warning threshold, not a limit: after it, the engine logs
`model_download_shutdown_waiting` and keeps waiting. It never abandons a worker
that could still change model files. How long this takes depends on the worker.
It is usually one chunk. The 30-second network timeout applies to each blocking
read, not to the whole shutdown, and checksumming stops between chunks. These
guarantees hold only while the process is allowed to finish. A supervisor that
kills the process first can leave a `.part` file, a promoted file whose metadata
was not recorded, or a registry record stuck at `downloading`. See
[deployment: graceful shutdown](deployment.md#graceful-shutdown--drain).

If the database is unavailable when a failure is recorded, the state change is
attempted once, not retried. The engine then logs
`model_download_state_not_recorded` with the intended status and the database
error's type (not its text), and the model can remain `downloading` in the
registry until an operator deletes or re-downloads it. The worker has still
stopped safely; only the durable record of its outcome is missing.

Limits:

- The model registry (and therefore the database and its backups) still stores
  the raw error text and the download URL as the source. API responses redact
  both, but anyone with direct access to the database can read them.
- Log lines, log exports and support bundles written before this change are not
  rewritten.
- Only the owned download task is covered. This is not a global exception or
  logging filter, and other components' logs are protected only by the JSON
  formatter's narrower query masking.
- Only credentials in URL userinfo or in credential-named query parameters are
  recognized. A credential in a URL path, in a parameter with an unrelated name,
  or encoded more than once inside a nested URL is not detected.

Until a release with this change is deployed, prefer model sources that do not
put credentials in the URL (for example a local import of a file fetched by an
approved process). There is no known incident. If credential-bearing URLs were
used with an earlier release, that warrants an authorized review of the affected
systems, time window and retained copies (logs, exports, support bundles,
backups), even if no leak was noticed: treat them as sensitive, and revoke or
rotate the affected credentials, or let signed URLs expire, following your
provider's procedures. Fixing the code does not revoke a credential or remove
copies already logged.

## Patch / CVE visibility

- CI runs **`pip-audit`** against the locked dependencies on every build and prints
  advisories to the run summary (advisory, non-gating — review and act on them).
- A **CycloneDX SBOM** is generated per build (`scripts/generate_sbom.py`) and
  uploaded as an artifact, so the exact component set of a release is inventoried.
- Runtime dependencies are version-floored in `pyproject.toml`; pin a full lock for
  reproducible deploys (see [docs/deployment.md](deployment.md)).

## Tamper-evident audit log

Security-relevant operator actions — API-key create/revoke/delete and model
load/unload/delete/import/download — are appended to a **hash-chained** audit log
(`audit_events`). Each row commits to the previous row's hash, so any later
insertion, deletion, or edit of a past row is detectable.

```
GET /admin/audit?verify=true
{
  "events": [ { "id", "ts", "actor", "action", "target", "detail", "hash" }, ... ],
  "verify": { "ok": true, "count": 42, "first_bad_id": null }
}
```

`actor` is the operator's API-key id, `local` (loopback dev), or `cli`. This is
tamper-**evident**, not tamper-**proof**: an attacker with full write access to the
database could recompute the whole chain. For stronger guarantees, ship the audit
log off-box (a later, ops-time concern). The chain reliably detects accidental
corruption and after-the-fact edits.
