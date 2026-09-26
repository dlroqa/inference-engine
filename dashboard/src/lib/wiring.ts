// The dashboard's wiring registry: for every control or panel, which backend
// endpoint it calls and which engine subsystems that endpoint drives.
//
// This is the single source of truth for the UI's "how this works" explanations.
// It is checked against the engine's real route inventory
// (routes.generated.json, produced by scripts/export_routes.py) and against the
// typed client in api.ts, so it cannot silently drift from the backend.

import type { api, SwitchName } from "./api";
import inventory from "./routes.generated.json";

export type Subsystem =
  | "gateway"
  | "scheduler"
  | "router"
  | "backend_pool"
  | "backend"
  | "model_registry"
  | "model_service"
  | "keystore"
  | "quota"
  | "billing"
  | "webhooks"
  | "client_events"
  | "audit"
  | "telemetry"
  | "log_buffer"
  | "store";

export const SUBSYSTEMS: Record<Subsystem, { label: string; description: string }> = {
  gateway: {
    label: "Gateway",
    description: "Authenticates keys, enforces rate/concurrency limits, quota, and billing entitlement.",
  },
  scheduler: {
    label: "Scheduler",
    description: "Global admission control: concurrency slots, a bounded wait queue, and drain.",
  },
  router: {
    label: "Router",
    description: "Maps a requested model name to backends: virtual models, route/cascade policies.",
  },
  backend_pool: {
    label: "Backend pool",
    description: "Health-aware placement across the primary and remote/external backends.",
  },
  backend: {
    label: "Backend",
    description: "The engine that generates tokens (llama.cpp locally, or a remote vLLM/SGLang).",
  },
  model_registry: {
    label: "Model registry",
    description: "SQLite catalog of imported/downloaded GGUF models with checksums and metadata.",
  },
  model_service: {
    label: "Model service",
    description:
      "Imports, downloads, loads, unloads, and deletes models. Records each file's SHA-256; a download is checked against an expected SHA-256 only when one is given.",
  },
  keystore: {
    label: "Keystore",
    description: "API keys stored as hashes; tokens are shown once at creation.",
  },
  quota: {
    label: "Quota & usage",
    description: "Per-key usage records and compute-unit windows (5-hour rolling, weekly).",
  },
  billing: {
    label: "Billing",
    description: "Plans, clients, subscriptions, and entitlements.",
  },
  webhooks: {
    label: "Webhooks",
    description: "Signed outbound event delivery with retries and dead-lettering.",
  },
  client_events: {
    label: "Client events",
    description: "Durable per-client event log served to clients over SSE.",
  },
  audit: {
    label: "Audit log",
    description: "Append-only, hash-chained record of operator and security actions.",
  },
  telemetry: {
    label: "Telemetry",
    description: "Counters, resource sampling, and the live metrics/feed event bus.",
  },
  log_buffer: {
    label: "Log buffer",
    description: "Structured request/error logs (metadata only, never prompts).",
  },
  store: {
    label: "Store",
    description: "The SQLite database and its migrations.",
  },
};

export type HttpMethod = "GET" | "POST" | "DELETE" | "WS";

export type ApiFn = keyof typeof api;

export interface WiringEntry {
  /** Stable id, e.g. "models.load". */
  id: string;
  /** Short control/panel name as shown in the UI. */
  label: string;
  /** What it does, in plain language. */
  what: string;
  /** Subsystems the request passes through, in order. */
  chain: Subsystem[];
  endpoint: { method: HttpMethod; path: string };
  /** The api.ts function that makes the call, or the live hook for WebSockets. */
  client: ApiFn | "useLiveMetrics" | "useLiveFeed";
  audited: boolean;
  /**
   * Config switches that must all be enabled for this action (read-only in the
   * UI). Turning off any one of them disables it. Names are keys of the
   * /admin/system `switches` object.
   */
  requiredSwitches?: SwitchName[];
  sideEffects?: string;
  docs?: DocRef;
}

/** A repository documentation page (and optional heading fragment). */
export interface DocRef {
  /** Repository-relative path, e.g. "docs/security.md#operator-access". */
  ref: string;
  /** Link text, e.g. "Security: operator access". */
  title: string;
}

// Documentation lives in the repository, not in the dashboard bundle, so links
// resolve to the canonical GitHub copy. `main` is a moving branch: the page can
// describe a newer engine than the one running. There is no version-aware docs
// base yet; see docs/dashboard.md.
export const DOCS_BASE = "https://github.com/dlroqa/inference-engine/blob/main/";

/** The URL for a registry docs reference. Only static registry text is used. */
export function docsUrl(doc: DocRef): string {
  if (!/^[A-Za-z0-9_./-]+\.md(#[a-z0-9-]+)?$/.test(doc.ref) || doc.ref.includes("..")) {
    throw new Error(`invalid docs reference: ${doc.ref}`);
  }
  return DOCS_BASE + doc.ref;
}

export const WIRING: WiringEntry[] = [
  {
    id: "app.identity",
    label: "Signed-in identity",
    what: "Checks the saved key and shows who the dashboard is acting as.",
    chain: ["gateway", "keystore"],
    endpoint: { method: "GET", path: "/admin/identity" },
    client: "identity",
    audited: false,
    docs: { ref: "docs/security.md#operator-access", title: "Security: operator access" },
  },
  {
    id: "app.system",
    label: "Feature-switch status",
    what: "Reads the engine's read-only configuration summary: feature switches (on/off only), readiness and drain state. The dashboard uses it to explain which actions a switch disables; the engine still enforces every switch itself.",
    chain: ["gateway", "store", "backend_pool", "scheduler"],
    endpoint: { method: "GET", path: "/admin/system" },
    client: "system",
    audited: false,
    docs: { ref: "docs/security.md#kill-switches", title: "Security: kill switches" },
  },
  {
    id: "overview.readiness",
    label: "Engine version and readiness",
    what: "Reports the engine version, database and migration checks.",
    chain: ["store", "backend"],
    endpoint: { method: "GET", path: "/admin/overview" },
    client: "overview",
    audited: false,
  },
  {
    id: "overview.metrics",
    label: "Live metrics",
    what: "Streams counters, resources, scheduler state, and energy (measured or unavailable).",
    chain: ["telemetry", "scheduler", "backend"],
    endpoint: { method: "WS", path: "/ws/metrics" },
    client: "useLiveMetrics",
    audited: false,
    docs: { ref: "docs/operability.md", title: "Operability" },
  },
  {
    id: "overview.metrics-fallback",
    label: "Metrics snapshot (polling fallback)",
    what: "Polls the same snapshot when the live stream is unavailable.",
    chain: ["telemetry"],
    endpoint: { method: "GET", path: "/metrics" },
    client: "metrics",
    audited: false,
  },
  {
    id: "overview.feed",
    label: "Inference live feed",
    what: "Streams request start/progress/end/error events (no prompt text).",
    chain: ["telemetry"],
    endpoint: { method: "WS", path: "/ws/feed" },
    client: "useLiveFeed",
    audited: false,
  },
  {
    id: "monitoring.alerts",
    label: "Alerts",
    what: "Suspended clients, quota thresholds, dead webhook deliveries, and unhealthy backends.",
    chain: ["billing", "webhooks", "backend_pool"],
    endpoint: { method: "GET", path: "/admin/alerts" },
    client: "alerts",
    audited: false,
    docs: { ref: "docs/monitoring.md", title: "Monitoring" },
  },
  {
    id: "monitoring.taxonomy",
    label: "Error taxonomy",
    what: "Counts errors by category over the chosen window.",
    chain: ["log_buffer"],
    endpoint: { method: "GET", path: "/admin/errors/taxonomy" },
    client: "errorTaxonomy",
    audited: false,
  },
  {
    id: "monitoring.attribution",
    label: "Key attribution",
    what: "Requests, tokens, and compute units per key over the chosen window.",
    chain: ["quota", "keystore", "billing"],
    endpoint: { method: "GET", path: "/admin/usage/attribution" },
    client: "usageAttribution",
    audited: false,
  },
  {
    id: "clients.list",
    label: "Clients",
    what: "Lists billing clients and their status.",
    chain: ["billing"],
    endpoint: { method: "GET", path: "/admin/billing/clients" },
    client: "listClients",
    audited: false,
    docs: { ref: "docs/billing.md", title: "Billing" },
  },
  {
    id: "clients.endpoints",
    label: "Webhook endpoints",
    what: "Lists a client's outbound webhook endpoints (secrets are never shown here).",
    chain: ["webhooks"],
    endpoint: { method: "GET", path: "/admin/billing/webhooks/endpoints" },
    client: "listWebhookEndpoints",
    audited: false,
    docs: { ref: "docs/webhooks.md", title: "Webhooks" },
  },
  {
    id: "clients.deliveries",
    label: "Recent deliveries",
    what: "Delivery attempts for each of the client's endpoints, filtered on the server.",
    chain: ["webhooks"],
    endpoint: { method: "GET", path: "/admin/billing/webhooks/deliveries" },
    client: "listDeliveries",
    audited: false,
  },
  {
    id: "models.list",
    label: "Models",
    what: "Lists registry models with download progress and host compatibility.",
    chain: ["model_registry", "model_service"],
    endpoint: { method: "GET", path: "/admin/models" },
    client: "listModels",
    audited: false,
    docs: { ref: "README.md#model-lifecycle-block-6", title: "README: model lifecycle" },
  },
  {
    id: "models.detail",
    label: "Model details",
    what: "Reads one registry model: status, compatibility, checksum, metadata and the credential-redacted source. A local import's path is not shown.",
    chain: ["model_registry", "model_service"],
    endpoint: { method: "GET", path: "/admin/models/{model_id}" },
    client: "getModel",
    audited: false,
    docs: { ref: "README.md#model-lifecycle-block-6", title: "README: model lifecycle" },
  },
  {
    id: "models.download",
    label: "Download model",
    what:
      "Starts a background download from Hugging Face or a URL and calculates its SHA-256. When you provide an expected SHA-256, the download is checked against it; a mismatch fails the download.",
    chain: ["model_service", "model_registry", "audit"],
    endpoint: { method: "POST", path: "/admin/models/download" },
    client: "downloadModel",
    audited: true,
    requiredSwitches: ["allow_model_management", "allow_network_downloads"],
    sideEffects: "Writes the file into the model store and a registry row.",
  },
  {
    id: "models.import",
    label: "Import model",
    what: "Registers a local GGUF file after hashing it.",
    chain: ["model_service", "model_registry", "audit"],
    endpoint: { method: "POST", path: "/admin/models/import" },
    client: "importModel",
    audited: true,
    requiredSwitches: ["allow_model_management"],
  },
  {
    id: "models.cancel",
    label: "Cancel download",
    what: "Asks an in-progress download to stop; it stops at its next checkpoint. Refused once the file is committed (the download is finishing).",
    chain: ["model_service"],
    endpoint: { method: "POST", path: "/admin/models/{model_id}/cancel" },
    client: "cancelDownload",
    audited: false,
  },
  {
    id: "models.load",
    label: "Load model",
    what: "Loads the model into the primary backend and makes it the active model.",
    chain: ["model_service", "backend_pool", "backend", "audit"],
    endpoint: { method: "POST", path: "/admin/models/{model_id}/load" },
    client: "loadModelById",
    audited: true,
    requiredSwitches: ["allow_model_management"],
    sideEffects: "Replaces the currently loaded model.",
  },
  {
    id: "models.unload",
    label: "Unload model",
    what: "Unloads the active model; inference for it returns 503 until reloaded.",
    chain: ["model_service", "backend", "audit"],
    endpoint: { method: "POST", path: "/admin/models/{model_id}/unload" },
    client: "unloadModelById",
    audited: true,
    requiredSwitches: ["allow_model_management"],
  },
  {
    id: "models.delete",
    label: "Delete model",
    what:
      "Removes the model's registry entry and stops any download in progress. Models must be unloaded first. Files managed by the engine are deleted; imported files outside the model store remain on disk.",
    chain: ["model_service", "model_registry", "audit"],
    endpoint: { method: "DELETE", path: "/admin/models/{model_id}" },
    client: "deleteModel",
    audited: true,
    requiredSwitches: ["allow_model_management"],
    sideEffects:
      "Deleted managed files cannot be restored by this action. An external imported file is preserved and can be registered again by importing it.",
  },
  {
    id: "logs.list",
    label: "Logs",
    what: "Recent structured log events, filterable by level and request id on the server.",
    chain: ["log_buffer"],
    endpoint: { method: "GET", path: "/logs" },
    client: "logs",
    audited: false,
    docs: { ref: "docs/operability.md", title: "Operability" },
  },
  {
    id: "security.audit",
    label: "Audit log and integrity check",
    what: "Lists audit records and, on demand, recomputes the hash chain.",
    chain: ["audit"],
    endpoint: { method: "GET", path: "/admin/audit" },
    client: "audit",
    audited: false,
    docs: { ref: "docs/security.md#tamper-evident-audit-log", title: "Security: tamper-evident audit log" },
  },
  {
    id: "keys.list",
    label: "API keys",
    what: "Lists keys by prefix, label, role, and status (never the token).",
    chain: ["keystore"],
    endpoint: { method: "GET", path: "/admin/keys" },
    client: "listKeys",
    audited: false,
  },
  {
    id: "keys.create",
    label: "Create key",
    what: "Creates an operator key; the token is shown once and only its hash is stored.",
    chain: ["keystore", "audit"],
    endpoint: { method: "POST", path: "/admin/keys" },
    client: "createKey",
    audited: true,
  },
  {
    id: "keys.revoke",
    label: "Revoke key",
    what: "Stops the key working immediately for every caller.",
    chain: ["keystore", "gateway", "audit"],
    endpoint: { method: "DELETE", path: "/admin/keys/{key_id}" },
    client: "revokeKey",
    audited: true,
  },
  {
    id: "keys.delete",
    label: "Delete revoked key",
    what: "Removes a revoked key's record; usage history keeps its id.",
    chain: ["keystore", "audit"],
    endpoint: { method: "DELETE", path: "/admin/keys/{key_id}" },
    client: "deleteKey",
    audited: true,
  },
];

// Backend routes the dashboard deliberately does not call (yet). Each has a
// reason; entries marked "planned" are removed as their views ship. This list is
// the honest inventory of what has no UI.
export const NOT_IN_UI: Record<string, string> = {
  "POST /v1/chat/completions": "Inference API for applications, not an operator control.",
  "POST /v1/messages": "Inference API for applications, not an operator control.",
  "GET /v1/models": "Inference API for applications, not an operator control.",
  "GET /client/me": "Client-facing API (client keys only).",
  "GET /client/plan": "Client-facing API (client keys only).",
  "GET /client/usage": "Client-facing API (client keys only).",
  "GET /client/services": "Client-facing API (client keys only).",
  "GET /client/events": "Client-facing API (client keys only).",
  "GET /client/events/stream": "Client-facing API (client keys only).",
  "POST /billing/webhooks/stripe": "Inbound Stripe webhook (signature-verified, not an operator call).",
  "GET /healthz": "Liveness probe for orchestrators; readiness is shown via /admin/overview.",
  "GET /readyz": "planned: System view (A3a).",
  "GET /version": "planned: System view (A3a); the version is shown via /admin/overview.",
  "GET /diagnostics": "planned: System view (A3a).",
  "GET /admin/backends": "planned: Backends & Routing view (A3a).",
  "GET /admin/routes": "planned: Backends & Routing view (A3a).",
  "POST /admin/route/plan": "planned: Backends & Routing view (A3a).",
  "POST /admin/model/load": "Legacy single-model control, superseded by /admin/models/{id}/load.",
  "POST /admin/model/unload": "Legacy single-model control, superseded by /admin/models/{id}/unload.",
  "GET /admin/billing/plans": "planned: plan administration (A3b).",
  "POST /admin/billing/plans": "planned: plan administration (A3b).",
  "POST /admin/billing/clients": "planned: client administration (A3b).",
  "POST /admin/billing/clients/{client_id}/keys": "planned: client key creation (A3b).",
  "GET /admin/billing/clients/{client_id}/reconcile": "planned: reconcile (A3b).",
  "POST /admin/billing/webhooks/endpoints": "planned: webhook administration (A3b).",
  "DELETE /admin/billing/webhooks/endpoints/{endpoint_id}": "planned: webhook administration (A3b).",
  "POST /admin/billing/webhooks/endpoints/{endpoint_id}/disable": "planned: webhook administration (A3b).",
  "POST /admin/billing/webhooks/endpoints/{endpoint_id}/rotate-secret": "planned: webhook administration (A3b).",
  "POST /admin/billing/webhooks/deliveries/{delivery_id}/replay": "planned: delivery replay (A3b).",
};

// Controls that make no request to the engine: they change what this browser
// shows (navigation, local filters, form tabs) or touch only browser storage.
// The dashboard labels them "No backend call" instead of leaving them unexplained.
export interface LocalControl {
  id: string;
  label: string;
  what: string;
  /** What the dashboard requests afterwards, when the control leads to a call. */
  followUp?: string;
}

export const LOCAL_CONTROLS: LocalControl[] = [
  {
    id: "local.navigation",
    label: "Navigation",
    what: "Changes the page address (#/view) in this browser.",
    followUp: "The view that opens then loads its own data from the engine.",
  },
  {
    id: "local.show-wiring",
    label: "Show wiring",
    what: "Shows or hides the endpoint (METHOD /path) next to every \"How this works\" button. The choice is kept in this browser only; it is off by default.",
  },
  {
    id: "local.saved-key",
    label: "Saved operator key",
    what: "Stores the key in this browser's local storage, or removes it.",
    followUp:
      "The dashboard then calls GET /admin/identity, sending the saved key (if any) as a Bearer token, to re-check who it is signed in as.",
  },
  {
    id: "local.add-source",
    label: "Model source tabs",
    what: "Switches which fields the form shows. Nothing is sent until you submit.",
  },
  {
    id: "local.logs-filter",
    label: "Text filter",
    what: "Filters the log events already loaded in this browser. The server is not queried again.",
  },
  {
    id: "local.copy-token",
    label: "Copy token",
    what: "Copies the new token to your clipboard. It is not sent anywhere and cannot be shown again.",
  },
  {
    id: "local.copy-checksum",
    label: "Copy checksum",
    what: "Copies the model's SHA-256 checksum text to your clipboard. Nothing is sent to the engine.",
  },
  {
    id: "local.close-drawer",
    label: "Close details",
    what: "Closes the model details and returns the page address to #/models (back to the list entry you came from, when you opened them from the list).",
  },
  {
    id: "local.close-detail",
    label: "Close detail",
    what: "Closes this panel and updates the page address.",
  },
  {
    id: "local.select-client",
    label: "Select a client",
    what: "Opens or closes the client's detail panel and updates the page address (#/clients/<id>). Click a row, or use the client's button with Enter or Space; keyboard focus stays on that button.",
    followUp: "The panel then loads the client's webhook endpoints and recent deliveries.",
  },
  {
    id: "local.dialog-cancel",
    label: "Cancel",
    what: "Closes this confirmation. Nothing is sent and nothing changes.",
  },
];

export interface RouteInfo {
  gate: string;
  module: string;
  handler: string;
}

const ROUTES = new Map<string, RouteInfo>(
  (inventory.routes as ({ method: string; path: string } & RouteInfo)[]).map((r) => [
    `${r.method} ${r.path}`,
    { gate: r.gate, module: r.module, handler: r.handler },
  ]),
);

/** The engine handler behind a wired endpoint, from the generated route inventory. */
export function routeFor(entry: WiringEntry): RouteInfo | undefined {
  return ROUTES.get(routeKey(entry.endpoint.method, entry.endpoint.path));
}

export type Explanation =
  | { kind: "wired"; entry: WiringEntry }
  | { kind: "local"; control: LocalControl };

/** Resolves a wiring or local-control id; throws on an unknown id. */
export function explain(id: string): Explanation {
  const entry = WIRING.find((w) => w.id === id);
  if (entry) return { kind: "wired", entry };
  const control = LOCAL_CONTROLS.find((c) => c.id === id);
  if (control) return { kind: "local", control };
  throw new Error(`unknown wiring id: ${id}`);
}

export function routeKey(method: string, path: string): string {
  return `${method} ${path}`;
}

export function wiringFor(id: string): WiringEntry {
  const entry = WIRING.find((w) => w.id === id);
  if (!entry) throw new Error(`unknown wiring id: ${id}`);
  return entry;
}
