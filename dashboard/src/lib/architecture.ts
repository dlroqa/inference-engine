// The Architecture view's node model: one contract per node (what it is, which
// engine code implements it, where its displayed values come from, and where
// its controls live) and a pure mapping from API observations to what each
// node shows.
//
// Pure functions only: no fetching, no React, no layout. The view's data hook
// supplies the observations, the diagram and the text list both render
// `deriveNodes()`, and docs/dashboard.md's generated table is rendered from
// `NODE_CONTRACTS` (lib/wiringDocs.ts), so the three cannot diverge.
//
// Status vocabulary (docs/architecture.md):
// - ok: observed working, from a live or fresh source.
// - attention: observed, and something needs the operator (draining, a backend
//   unavailable, dead-lettered deliveries, a failing readiness check).
// - off: disabled by a feature switch. Not healthy, not empty.
// - unknown: not observed: loading, the source failed, or nothing reports it.
//   A failed read is a failure to observe, never proof the subsystem is down.
// - info: configuration or a static description; no live probe exists.

import type {
  Alert,
  BackendsPage,
  MetricsSnapshot,
  ModelInfo,
  SystemInfo,
} from "./api";
import { SUBSYSTEMS, type Subsystem } from "./wiring";

/** The diagram's entry node plus every engine subsystem in the wiring registry. */
export type NodeId = "edges" | Subsystem;

/** Dashboard views a node can open (each exists in the shell's navigation). */
export type ArchDestination =
  | "overview"
  | "monitoring"
  | "logs"
  | "system"
  | "models"
  | "routing"
  | "clients"
  | "keys"
  | "security";

export const DESTINATION_LABELS: Record<ArchDestination, string> = {
  overview: "Overview",
  monitoring: "Monitoring",
  logs: "Logs",
  system: "System",
  models: "Models",
  routing: "Backends & routing",
  clients: "Clients",
  keys: "API keys",
  security: "Security",
};

/** Observation sources, each an existing operator endpoint or the live hook. */
export type SourceId = "system" | "metrics" | "backends" | "models" | "alerts";

export const SOURCE_LABELS: Record<SourceId, string> = {
  system: "GET /admin/system",
  metrics: "WS /ws/metrics (GET /metrics fallback)",
  backends: "GET /admin/backends",
  models: "GET /admin/models",
  alerts: "GET /admin/alerts",
};

export interface NodeContract {
  id: NodeId;
  label: string;
  description: string;
  /** On the request path (left to right), or a side system. */
  lane: "path" | "side";
  /** Engine modules that implement it. */
  modules: string[];
  /** How it relates to the request path and the other nodes (inspected call paths). */
  relation: string;
  /** Where its displayed values come from; empty for a static description. */
  sources: SourceId[];
  /** Exact response fields used, and what they mean. */
  fields: string;
  /** What is shown when a source is loading, failed or stale. */
  unavailable: string;
  /** The view with its controls, or null with the reason there is none. */
  destination: ArchDestination | null;
  noDestinationReason?: string;
}

const METRICS_UNAVAILABLE =
  "Unknown while connecting or when both the stream and polling fail; values from a lost connection are marked stale with their observation time.";
const SYSTEM_UNAVAILABLE =
  "Unknown until /admin/system answers; after a failed refresh the last summary is kept and marked stale.";

function sub(id: Subsystem): { label: string; description: string } {
  return SUBSYSTEMS[id];
}

export const NODE_CONTRACTS: NodeContract[] = [
  {
    id: "edges",
    label: "API edges",
    description: "OpenAI and Anthropic HTTP APIs, and the optional gRPC service. They share one serving path.",
    lane: "path",
    modules: ["engine/api/openai_router.py", "engine/api/anthropic_router.py", "engine/grpc", "engine/api/serving.py"],
    relation: "Entry point. Every generation request goes to the gateway, then the scheduler, router, pool and backend.",
    sources: ["system"],
    fields: "switches.grpc_enabled, metadata.grpc_port. The HTTP edges are always mounted.",
    unavailable: "HTTP edges are described statically; gRPC is unknown until /admin/system answers.",
    destination: "monitoring",
  },
  {
    id: "gateway",
    ...sub("gateway"),
    lane: "path",
    modules: ["engine/gateway.py"],
    relation: "Authenticates the key (keystore), applies rate/concurrency limits and quota (usage), and the plan entitlement (billing), before admission.",
    sources: ["system"],
    fields: "switches.require_auth, switches.ip_allowlist_set (configuration, not a security verdict).",
    unavailable: SYSTEM_UNAVAILABLE,
    destination: "keys",
  },
  {
    id: "scheduler",
    ...sub("scheduler"),
    lane: "path",
    modules: ["engine/inference/scheduler.py"],
    relation: "Admits an authorized request into a concurrency slot or the bounded queue, then hands it to the router.",
    sources: ["metrics", "system"],
    fields: "scheduler.in_use, max_concurrency, queue_depth, admitted_total, rejected_total, wait_ms_avg / wait_ms_max (averages and maxima since start, no percentiles); draining from /admin/system.",
    unavailable: METRICS_UNAVAILABLE,
    destination: "overview",
  },
  {
    id: "router",
    ...sub("router"),
    lane: "path",
    modules: ["engine/inference/router.py"],
    relation: "Resolves the requested model name (virtual models, route/cascade policies, workload rules) to backends in the pool.",
    sources: ["system"],
    fields: "routing.virtual_models (name, policy), routing.workload_routing_enabled, routing.workload_rule_count (configuration).",
    unavailable: SYSTEM_UNAVAILABLE,
    destination: "routing",
  },
  {
    id: "backend_pool",
    ...sub("backend_pool"),
    lane: "path",
    modules: ["engine/inference/registry.py"],
    relation: "Places the request on a ready backend with capacity; spillover providers only when no local backend can admit. Chosen once, at admission.",
    sources: ["backends"],
    fields: "backends[].available, tier, external, in_flight / max_in_flight; the count is every backend the engine returns (the whole pool).",
    unavailable: "Unknown until /admin/backends answers; after a failed refresh the last list is kept and marked stale.",
    destination: "routing",
  },
  {
    id: "backend",
    ...sub("backend"),
    lane: "path",
    modules: ["engine/inference/llamacpp.py", "engine/inference/remote"],
    relation: "Generates tokens for the placed request and streams them back through the edge.",
    sources: ["system", "backends"],
    fields: "readiness.inference.available / model_id / reason from /admin/system; backends[].kind and location (configured adapters, not a hardware qualification).",
    unavailable: SYSTEM_UNAVAILABLE,
    destination: "models",
  },
  {
    id: "keystore",
    ...sub("keystore"),
    lane: "side",
    modules: ["engine/auth/keys.py"],
    relation: "Read by the gateway to authenticate each request; managed from API keys.",
    sources: [],
    fields: "None (no key counts are shown here).",
    unavailable: "Static description.",
    destination: "keys",
  },
  {
    id: "quota",
    ...sub("quota"),
    lane: "side",
    modules: ["engine/quota/store.py", "engine/quota/windows.py"],
    relation: "Written by the gateway after each request; read by the gateway for quota and by Monitoring for attribution.",
    sources: [],
    fields: "None (usage is per key and per window; Monitoring shows it).",
    unavailable: "Static description.",
    destination: "monitoring",
  },
  {
    id: "billing",
    ...sub("billing"),
    lane: "side",
    modules: ["engine/billing/store.py", "engine/billing/stripe.py"],
    relation: "Supplies each owned key's plan entitlement to the gateway. Inbound billing lifecycle events update it and emit client events.",
    sources: ["system"],
    fields: "metadata.billing_provider (configuration).",
    unavailable: SYSTEM_UNAVAILABLE,
    destination: "clients",
  },
  {
    id: "client_events",
    ...sub("client_events"),
    lane: "side",
    modules: ["engine/billing/client_events.py"],
    relation: "Account events (lifecycle, usage thresholds) are recorded here for client SSE; the same emitter hands them to webhooks.",
    sources: ["system"],
    fields: "switches.client_events_enabled.",
    unavailable: SYSTEM_UNAVAILABLE,
    destination: null,
    noDestinationReason:
      "No operator control: clients read their own events over SSE. It is switched by client_events_enabled (shown in System).",
  },
  {
    id: "webhooks",
    ...sub("webhooks"),
    lane: "side",
    modules: ["engine/billing/webhooks"],
    relation: "Receives account events from the emitter and delivers them to registered endpoints (no redirects, ASCII hosts only).",
    sources: ["system", "alerts"],
    fields: "switches.webhooks_enabled; the webhook_dead_letters alert from /admin/alerts, derived from the engine-wide count of dead deliveries (its message is shown as is).",
    unavailable: "Delivery state is unknown until /admin/system answers; the dead-letter state is unknown until /admin/alerts answers. \"None dead-lettered\" is shown only from a successful alerts read.",
    destination: "clients",
  },
  {
    id: "audit",
    ...sub("audit"),
    lane: "side",
    modules: ["engine/audit/log.py"],
    relation: "Records operator and security writes (keys, models, billing, webhooks). Not on the request path.",
    sources: [],
    fields: "None: the chain is not verified by this view.",
    unavailable: "Always \"not checked here\". Security runs the verification; a result covers the rows present when it ran.",
    destination: "security",
  },
  {
    id: "telemetry",
    ...sub("telemetry"),
    lane: "side",
    modules: ["engine/telemetry"],
    relation: "Fed by the serving path (counters, scheduler, backend state); streamed on /ws/metrics and /ws/feed.",
    sources: ["metrics"],
    fields: "The live-metrics connection state and energy.state / watts / source / reason.",
    unavailable: METRICS_UNAVAILABLE,
    destination: "monitoring",
  },
  {
    id: "log_buffer",
    ...sub("log_buffer"),
    lane: "side",
    modules: ["engine/telemetry/logbuffer.py"],
    relation: "Collects structured request and error logs (metadata only) from every component; persisted to the store while this engine owns it.",
    sources: [],
    fields: "None.",
    unavailable: "Static description.",
    destination: "logs",
  },
  {
    id: "model_registry",
    ...sub("model_registry"),
    lane: "side",
    modules: ["engine/models/registry.py"],
    relation: "Catalog written by the model service; the loaded entry is what the local backend serves.",
    sources: ["models"],
    fields: "models[].status and loaded, over the whole registry (GET /admin/models is not paginated).",
    unavailable: "Unknown until /admin/models answers; after a failed refresh the last list is kept and marked stale.",
    destination: "models",
  },
  {
    id: "model_service",
    ...sub("model_service"),
    lane: "side",
    modules: ["engine/models/service.py"],
    relation: "Imports and downloads into the registry, and loads or unloads the local backend.",
    sources: ["system"],
    fields: "switches.allow_model_management, switches.allow_network_downloads.",
    unavailable: SYSTEM_UNAVAILABLE,
    destination: "models",
  },
  {
    id: "store",
    ...sub("store"),
    lane: "side",
    modules: ["engine/store"],
    relation: "One SQLite file holds keys, usage, billing, webhooks, client events, the audit log, the model registry and persisted logs. One engine per data directory; no replication.",
    sources: ["system"],
    fields: "readiness.checks.database and readiness.checks.migrations (the /readyz probe's own checks).",
    unavailable: SYSTEM_UNAVAILABLE,
    destination: "system",
  },
];

export const CONTRACTS: Record<NodeId, NodeContract> = Object.fromEntries(
  NODE_CONTRACTS.map((c) => [c.id, c]),
) as Record<NodeId, NodeContract>;

/** Connections drawn in the diagram (inspected call paths). */
export interface ArchEdge {
  from: NodeId;
  to: NodeId;
  kind: "path" | "side";
}

export const ARCH_EDGES: ArchEdge[] = [
  { from: "edges", to: "gateway", kind: "path" },
  { from: "gateway", to: "scheduler", kind: "path" },
  { from: "scheduler", to: "router", kind: "path" },
  { from: "router", to: "backend_pool", kind: "path" },
  { from: "backend_pool", to: "backend", kind: "path" },
  { from: "gateway", to: "keystore", kind: "side" },
  { from: "gateway", to: "quota", kind: "side" },
  { from: "gateway", to: "billing", kind: "side" },
  { from: "billing", to: "client_events", kind: "side" },
  { from: "client_events", to: "webhooks", kind: "side" },
  { from: "scheduler", to: "telemetry", kind: "side" },
  { from: "model_service", to: "model_registry", kind: "side" },
  { from: "model_service", to: "backend", kind: "side" },
];

/** Legacy illustrative focus names mapped to canonical node ids. */
const FOCUS_ALIASES: Record<string, NodeId> = {
  models: "model_registry",
  pool: "backend_pool",
  backends: "backend_pool",
  keys: "keystore",
  sqlite: "store",
  edge: "edges",
};

/** The node a `focus` parameter names, or null when it names none. */
export function resolveFocus(value: string | null | undefined): NodeId | null {
  if (!value) return null;
  if (Object.prototype.hasOwnProperty.call(CONTRACTS, value)) return value as NodeId;
  return Object.prototype.hasOwnProperty.call(FOCUS_ALIASES, value) ? FOCUS_ALIASES[value] : null;
}

// --- Observations -----------------------------------------------------------

export type SourceStatus = "loading" | "ready" | "error";

/** One fetched source. `stale`: `data` is from an earlier read and the latest failed. */
export interface Source<T> {
  status: SourceStatus;
  data: T | null;
  error: string | null;
  /** Epoch seconds when `data` was received. */
  observedAt: number | null;
  stale: boolean;
}

export interface MetricsObservation {
  snapshot: MetricsSnapshot | null;
  /** The live hook's transport state. */
  status: "connecting" | "live" | "polling" | "down";
  fresh: boolean;
}

export interface Observations {
  system: Source<SystemInfo>;
  metrics: MetricsObservation;
  backends: Source<BackendsPage>;
  models: Source<{ models: ModelInfo[] }>;
  alerts: Source<{ alerts: Alert[] }>;
}

export type Tone = "ok" | "attention" | "off" | "unknown" | "info";

export const TONE_LABELS: Record<Tone, string> = {
  ok: "OK",
  attention: "Needs attention",
  off: "Off",
  unknown: "Unknown",
  info: "Configuration",
};

export interface NodeStatus {
  tone: Tone;
  /** One line, shown on the node. */
  summary: string;
  /** Further observed facts, shown in the details. */
  facts: string[];
  /** Epoch seconds of the observation behind the status, when there is one. */
  observedAt: number | null;
  /** The values are from an earlier observation and the latest one failed or was lost. */
  stale: boolean;
}

export interface ArchNode extends NodeContract {
  status: NodeStatus;
}

function status(tone: Tone, summary: string, facts: string[] = [], at: number | null = null, stale = false): NodeStatus {
  return { tone, summary, facts, observedAt: at, stale };
}

function notObserved<T>(src: Source<T>, what: string): NodeStatus {
  if (src.status === "loading") return status("unknown", `Loading ${what}…`);
  return status("unknown", `Not observed: ${what} could not be read`, src.error ? [src.error] : []);
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function withSystem(obs: Observations, f: (s: SystemInfo) => NodeStatus): NodeStatus {
  const src = obs.system;
  if (!src.data) return notObserved(src, "the system summary");
  const out = f(src.data);
  return { ...out, observedAt: src.observedAt, stale: src.stale };
}

function metricsState(m: MetricsObservation): NodeStatus | null {
  if (m.snapshot) return null;
  if (m.status === "down") return status("unknown", "Not observed: live metrics are unavailable");
  return status("unknown", "Waiting for live metrics…");
}

const derive: Record<NodeId, (obs: Observations) => NodeStatus> = {
  edges: (obs) => {
    const http = "HTTP: OpenAI /v1/chat/completions, Anthropic /v1/messages";
    if (!obs.system.data) {
      const s = notObserved(obs.system, "the gRPC setting");
      return status("unknown", s.summary, [http, "gRPC: unknown", ...s.facts]);
    }
    return withSystem(obs, (s) =>
      status("info", s.switches.grpc_enabled ? `HTTP + gRPC (port ${s.metadata.grpc_port ?? "unknown"})` : "HTTP only (gRPC off)", [
        http,
        s.switches.grpc_enabled ? `gRPC: on, port ${s.metadata.grpc_port ?? "unknown"}` : "gRPC: off (grpc_enabled=false)",
      ]),
    );
  },
  gateway: (obs) =>
    withSystem(obs, (s) =>
      status("info", s.switches.require_auth ? "API keys required" : "API keys not required (require_auth=false)", [
        s.switches.ip_allowlist_set ? "IP allowlist: set" : "IP allowlist: not set",
        "Limits, quota and plan entitlements are enforced per request",
      ]),
    ),
  scheduler: (obs) => {
    const m = obs.metrics;
    const waiting = metricsState(m);
    if (waiting) return waiting;
    const sch = m.snapshot?.scheduler ?? null;
    const at = m.snapshot?.ts ?? null;
    if (!sch) return status("unknown", "Not reported by this engine", [], at, !m.fresh);
    const facts = [
      `${plural(sch.admitted_total, "request")} admitted, ${sch.rejected_total} rejected since start`,
      sch.wait_ms_avg === null
        ? "Queue wait: no samples yet"
        : `Queue wait: average ${Math.round(sch.wait_ms_avg)} ms, maximum ${Math.round(sch.wait_ms_max)} ms`,
    ];
    const summary = `${sch.in_use}/${sch.max_concurrency} in use · ${sch.queue_depth} queued`;
    if (obs.system.data?.draining) {
      return status("attention", `Draining · ${summary}`, ["New requests are refused while the engine drains", ...facts], at, !m.fresh);
    }
    return status("ok", summary, facts, at, !m.fresh);
  },
  router: (obs) =>
    withSystem(obs, (s) => {
      const r = s.routing;
      const vms = r.virtual_models;
      return status("info", vms.length === 0 ? "No virtual models" : plural(vms.length, "virtual model"), [
        ...vms.map((v) => `${v.name}: ${v.policy}`),
        r.workload_routing_enabled ? `Workload routing on (${plural(r.workload_rule_count, "rule")})` : "Workload routing off",
      ]);
    }),
  backend_pool: (obs) => {
    const src = obs.backends;
    if (!src.data) return notObserved(src, "the backend pool");
    const list = src.data.backends;
    const available = list.filter((b) => b.available).length;
    const spill = list.filter((b) => b.tier === "spillover").length;
    const inFlight = list.reduce((n, b) => n + b.in_flight, 0);
    const facts = [
      `${list.length - spill} primary, ${spill} spillover`,
      `${inFlight} request${inFlight === 1 ? "" : "s"} in flight`,
    ];
    let out: NodeStatus;
    if (list.length === 0) out = status("attention", "No backends reported", facts);
    else if (available < list.length)
      out = status("attention", `${available} of ${plural(list.length, "backend")} available`, facts);
    else out = status("ok", `${available} of ${plural(list.length, "backend")} available`, facts);
    return { ...out, observedAt: src.observedAt, stale: src.stale };
  },
  backend: (obs) =>
    withSystem(obs, (s) => {
      const inf = s.readiness.inference;
      const kinds = obs.backends.data
        ? [...new Set(obs.backends.data.backends.map((b) => `${b.kind} (${b.location})`))].join(", ") || "none"
        : "unknown";
      const facts = [`Configured adapters: ${kinds}`, "Adapters are configuration; this view does not qualify hardware"];
      if (inf.available) return status("ok", `Serving ${inf.model_id ?? "a model"}`, facts);
      return status("attention", `Not serving: ${inf.reason ?? inf.state ?? "no model loaded"}`, facts);
    }),
  keystore: () => status("info", "Keys stored as hashes; tokens shown once", ["Key lists and revocation are in API keys"]),
  quota: () => status("info", "Per-key usage, 5-hour and weekly windows", ["Usage by key is in Monitoring"]),
  billing: (obs) =>
    withSystem(obs, (s) =>
      status(
        "info",
        s.metadata.billing_provider ? `Provider: ${s.metadata.billing_provider}` : "No billing provider configured",
        ["Plans, clients and entitlements are managed in Clients"],
      ),
    ),
  client_events: (obs) =>
    withSystem(obs, (s) =>
      s.switches.client_events_enabled
        ? status("info", "Client event log on (SSE)")
        : status("off", "Client event log off (client_events_enabled=false)"),
    ),
  webhooks: (obs) => {
    if (!obs.system.data) return notObserved(obs.system, "the webhook setting");
    const enabled = obs.system.data.switches.webhooks_enabled;
    const al = obs.alerts;
    const dead = al.data?.alerts.find((a) => a.kind === "webhook_dead_letters") ?? null;
    const deadFact = !al.data
      ? al.status === "loading"
        ? "Dead-letter state: loading…"
        : "Dead-letter state: not observed (alerts could not be read)"
      : dead
        ? dead.message
        : "No dead-lettered deliveries";
    const at = al.data ? al.observedAt : obs.system.observedAt;
    const stale = obs.system.stale || al.stale;
    if (!enabled) return status("off", "Delivery off (webhooks_enabled=false)", [deadFact], at, stale);
    if (!al.data) return status("unknown", "Delivery on · dead letters not observed", [deadFact], at, stale);
    if (dead) return status("attention", dead.message, ["Delivery on"], at, stale);
    return status("ok", "Delivery on · none dead-lettered", [], at, stale);
  },
  audit: () =>
    status("unknown", "Chain not checked here", [
      "Verify the hash chain in Security. A result covers the rows present when it ran.",
    ]),
  telemetry: (obs) => {
    const m = obs.metrics;
    const e = m.snapshot?.energy;
    const energy = !e
      ? []
      : e.state === "measured"
        ? [`Energy: ${e.watts ?? "?"} W (${e.source ?? "unknown source"})`]
        : [`Energy: not measured${e.reason ? ` (${e.reason})` : ""}`];
    const at = m.snapshot?.ts ?? null;
    if (m.status === "live" && m.fresh) return status("ok", "Live stream connected", energy, at);
    if (m.status === "polling" && m.fresh) return status("ok", "Polling /metrics (stream unavailable)", energy, at);
    if (m.status === "down")
      return status("unknown", "Not observed: stream and polling failed", energy, at, m.snapshot !== null);
    return status("unknown", "Connecting…", energy, at, m.snapshot !== null);
  },
  log_buffer: () => status("info", "Request and error logs, metadata only", ["Never prompts or completions"]),
  model_registry: (obs) => {
    const src = obs.models;
    if (!src.data) return notObserved(src, "the model registry");
    const list = src.data.models;
    const loaded = list.filter((m) => m.loaded).length;
    const errors = list.filter((m) => m.status === "error").length;
    const busy = list.filter((m) => m.status === "downloading" || m.status === "verifying").length;
    const facts = [`${busy} downloading or verifying`, `${errors} in error`];
    const summary = list.length === 0 ? "No models registered" : `${plural(list.length, "model")} · ${loaded} loaded`;
    return status("info", summary, facts, src.observedAt, src.stale);
  },
  model_service: (obs) =>
    withSystem(obs, (s) => {
      const downloads = s.switches.allow_network_downloads ? "Network downloads: on" : "Network downloads: off (allow_network_downloads=false)";
      if (!s.switches.allow_model_management) {
        return status("off", "Model management off (allow_model_management=false)", [downloads]);
      }
      return status("info", "Model management on", [downloads]);
    }),
  store: (obs) =>
    withSystem(obs, (s) => {
      const db = s.readiness.checks.database ?? "not reported";
      const mig = s.readiness.checks.migrations ?? "not reported";
      const summary = `SQLite · database ${db} · migrations ${mig}`;
      const facts = ["Single SQLite file; no replication or high availability"];
      return db === "ok" && mig === "applied" ? status("ok", summary, facts) : status("attention", summary, facts);
    }),
};

/** Every node, in contract order, with its status derived from `obs`. */
export function deriveNodes(obs: Observations): ArchNode[] {
  return NODE_CONTRACTS.map((c) => ({ ...c, status: derive[c.id](obs) }));
}
