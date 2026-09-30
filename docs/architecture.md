# Architecture

How a request moves through the engine, the systems beside that path, and what
is implemented versus what has actually been verified. The dashboard's
**Architecture** view (`#/architecture`) shows the same model live.

## Request path and side systems

```text
API edges → Gateway → Scheduler → Router → Backend pool → Backend
 (HTTP,      (auth,     (slots,     (virtual   (placement,     (llama.cpp
  gRPC)      limits,    queue,      models,    spillover        in-process,
             quota,     drain)      policies)  last)            or remote)
             plans)
```

These are the calls in `engine/api/serving.py`: `gateway.authorize_token`,
`scheduler.admit`, `router.acquire` (which places on the pool) and
`backend.generate`. The OpenAI and Anthropic HTTP edges and the gRPC edge share
that one path.

Side systems:

- **Keystore, Quota & usage, Billing**: read (and, for usage, written) by the
  gateway for every request.
- **Client events** and **Webhooks**: one emitter fans account events
  (billing lifecycle, usage thresholds) out to the client SSE log and to
  outbound webhooks. Each is switched separately.
- **Audit log**: records operator and security writes. It is not on the
  request path.
- **Telemetry** and **Log buffer**: fed by the serving path; metrics stream on
  `/ws/metrics`, logs are metadata only.
- **Model service** and **Model registry**: import, download, load and unload;
  the loaded registry entry is what the local backend serves.
- **Store**: one SQLite file for all of the above that is persisted. One
  engine per data directory (lock files); no replication or high availability.

Each node's contract (implementing modules, relationship, the exact response
fields it shows, what happens when a source is unavailable, and which view
holds its controls) is the generated **Architecture nodes** table in
[the dashboard's wiring table](dashboard.md#ui--subsystem--endpoint-table).
It is rendered from `dashboard/src/lib/architecture.ts`, the same contracts the
view uses, and checked for drift in CI.

## What the Architecture view shows

The view is an operator-only, read-only explanation. It adds no endpoint and no
control: it reads `GET /admin/system`, `GET /admin/backends`,
`GET /admin/models`, `GET /admin/alerts` and the live metrics stream, and each
node opens the view that holds its controls.

**Status words** (each shown as an icon plus text, never colour alone):

| Status | Meaning |
| --- | --- |
| OK | Observed working, from a current observation |
| Needs attention | Observed, and something needs the operator: draining, a backend unavailable, dead-lettered deliveries, a failing readiness check |
| Off | Disabled by a feature switch. Not healthy, not empty |
| Unknown | Not observed: loading, a failed read, or nothing reports it. A failed read is a failure to observe, not proof the subsystem is down |
| Configuration | Configuration or a static description; no live probe exists |

**Freshness and scope.**

- Backends, models and alerts are read when the view opens and on Refresh;
  the system summary is the session's shared copy; metrics are live (stream,
  or polling when the stream is unavailable).
- Every observed status shows when it was observed. After a failed refresh or
  a lost connection, the last values stay, marked **stale** with that time.
- Counts come only from sources whose scope is known: the backend count is the
  whole pool `/admin/backends` returns, the model count the whole registry.
  "None dead-lettered" is shown only from a successful `/admin/alerts` read,
  whose webhook alert comes from the engine-wide dead-letter count.
- Scheduler figures are averages and maxima since start; no percentiles.
- The audit chain is "not checked here". Verification runs in Security, and a
  result covers the rows present when it ran.
- Adapter kinds are configuration. The view does not qualify hardware.

**Session safety.** Every read belongs to the view that issued it and to the
current operator session. Leaving the view, Refresh, changing or forgetting the
key, or losing access makes older responses (successes and failures) void:
they change nothing and cannot end the new session. No token, secret, prompt,
completion, credential-bearing URL or filesystem path is shown.

**Navigation.** `#/architecture?focus=<node>` selects a node (canonical ids as
in the table; a few older names such as `models` are accepted as aliases). An
unknown name shows a notice and the whole architecture. Following a link moves
focus to the node's details; selecting a node in the view updates the address
without adding a history entry and without moving focus. In the signed-in
dashboard, each subsystem in a "How this works" popover links to its node
(not inside confirmation dialogs).

**Layout.** At 1200 px and wider the diagram is shown above the text list.
Narrower, the text list, which carries everything the diagram does, is the
presentation; the page never scrolls sideways.

## Support matrix

"Implemented" means the code exists on `main`. "Verified" names what CI
actually ran. Neither means deployed: deploying is an operator action.

| Capability | Implemented | Verified in CI | Not qualified / remaining |
| --- | --- | --- | --- |
| Release image, Linux x86-64 with AVX2 | Yes | Docker build and start smoke (CI); Release: the candidate image under the hardened Compose file (no capabilities, `no-new-privileges`), persistence, backup/restore, ownership/recovery; Critical-only scan gate | A passing gate means no unexcepted Critical finding, not "no vulnerabilities" (see [triage](security/candidate-image-triage.md)) |
| llama.cpp local backend | Yes, in-process | A real tiny GGUF on an AVX2 runner: generation, OpenAI and Anthropic SDKs, structured output, model lifecycle, bounded concurrency | Larger models and performance are not qualified. Generation runs in worker threads: no process-level crash isolation |
| vLLM / SGLang adapters | Yes (`remote_vllm`, `remote_sglang`) | `scripts/remote_smoke.py` against llama.cpp's OpenAI-compatible server (no GPU) | Not qualified on real vLLM/SGLang or GPUs. Remote structured output is not supported |
| External spillover | Yes, off by default, allowlisted | In-process tests with a fake spillover backend | No real provider is called in CI. Admission-time only; no failover after generation starts |
| gRPC edge | Yes, off by default | In-process server tests | Not exercised in the image end-to-end test |
| Windows and macOS | Package only | Wheel build and the portable test suite | No native model install, service management, upgrade, rollback or uninstall. The release image does not support them |
| Six native install targets (Linux, macOS, Windows × x86-64, arm64) | No | None | Part B, after its Phase 0 feasibility report |
| Store | SQLite, one engine per data directory | Migrations, locking, backup/restore (image test) | No PostgreSQL, replication or high availability |
| Model checksums | SHA-256 recorded; checked against an expected value only when one is given | Download and import tests | A recorded checksum is not an independent authenticity check |
| Audit log | Hash-chained, verified on demand | Chain and tamper tests | Detects changes to the chain; it is not a security certification |
| Dependency audits | `pip-audit` and `npm audit` in CI | Both are advisory: a green job can contain findings | The Release gate (Critical only) is the only vulnerability gate |

## Open security follow-ups

Tracked with owners and review points in
[the triage](security/candidate-image-triage.md#5-follow-ups): F1 and F6 are
integrated (F1 not deployed; F6's failing-gate path not exercised), F3/F4a are
partially investigated, and F2, F4b and F5 are open. F4b monitoring has no
accepted owner and is not scheduled. No proposed disposition is accepted by
merging code.

## Deployment note: webhook endpoints with non-ASCII hosts

From `1f3bbe0` on, such endpoints are never delivered to. Register the `xn--`
form, configure the receiver with its new signing secret, then delete the old
endpoint; dead deliveries do not move. See
[Deployment: upgrading](deployment.md#upgrading-to-a-release-that-includes-1f3bbe0-a3b-or-later).
