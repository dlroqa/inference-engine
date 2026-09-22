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
