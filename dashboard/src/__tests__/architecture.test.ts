// @vitest-environment node
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { Alert, BackendRow, MetricsSnapshot, ModelInfo, SchedulerPanel, SystemInfo } from "../lib/api";
import {
  ARCH_EDGES,
  CONTRACTS,
  DESTINATION_LABELS,
  NODE_CONTRACTS,
  deriveNodes,
  resolveFocus,
  type NodeId,
  type Observations,
  type Source,
} from "../lib/architecture";
import { SUBSYSTEMS } from "../lib/wiring";

// The node model's contract distinctions: unknown is never OK, a disabled
// feature is not healthy, a count needs a known scope, and stale values keep
// their observation time. Pure: no rendering.

const REPO = fileURLToPath(new URL("../../../", import.meta.url).href);

const sysInfo = (over: Partial<SystemInfo> = {}, switches: Partial<SystemInfo["switches"]> = {}): SystemInfo => ({
  build: { version: "0.2.0", commit: null, built_at: null },
  readiness: {
    ready: true,
    checks: { database: "ok", migrations: "applied" },
    inference: { available: true, model_id: "tiny", state: "ready" },
  },
  draining: false,
  switches: {
    allow_model_management: true,
    allow_network_downloads: true,
    allow_structured_output: true,
    diagnostics_enabled: true,
    require_auth: true,
    webhooks_enabled: true,
    client_events_enabled: true,
    ip_allowlist_set: false,
    grpc_enabled: false,
    ...switches,
  },
  metadata: { grpc_port: null, billing_provider: null },
  routing: {
    backend_kind: "llamacpp",
    virtual_models: [],
    workload_routing_enabled: false,
    workload_rule_count: 0,
    remote_workers: [],
    spillover_providers: [],
  },
  ...over,
});

const backend = (over: Partial<BackendRow> = {}): BackendRow => ({
  name: "primary",
  kind: "llamacpp",
  location: "local",
  state: "ready",
  available: true,
  in_flight: 0,
  max_in_flight: 1,
  model_id: "tiny",
  served_model: null,
  context_length: 2048,
  supports_prefix_cache: false,
  supports_kv_cache_metrics: false,
  tier: "primary",
  external: false,
  ...over,
});

const scheduler = (over: Omit<Partial<SchedulerPanel>, "draining"> & { draining?: unknown } = {}): SchedulerPanel =>
  ({
  draining: false,
  max_concurrency: 4,
  max_queue_depth: 16,
  queue_timeout_s: 30,
  in_use: 2,
  available: 2,
  queue_depth: 3,
  peak_in_use: 4,
  peak_queue_depth: 5,
  admitted_total: 10,
  rejected_total: 1,
  rejected_queue_full: 1,
  rejected_timeout: 0,
  cancelled_total: 0,
  slow_consumer_total: 0,
  wait_ms_avg: 12.4,
  wait_ms_max: 80,
  wait_ms_last: 5,
  ...over,
  }) as SchedulerPanel;

const snapshot = (sch: SchedulerPanel | null = scheduler()): MetricsSnapshot =>
  ({
    ts: 1_700_000_000,
    scheduler: sch,
    energy: { state: "unavailable", watts: null, j_per_token: null, tokens_per_joule: null, source: null, reason: "no validated probe" },
  }) as unknown as MetricsSnapshot;

const ready = <T>(data: T, at = 1_700_000_100): Source<T> => ({
  status: "ready",
  data,
  error: null,
  observedAt: at,
  stale: false,
});
const loading = <T>(): Source<T> => ({ status: "loading", data: null, error: null, observedAt: null, stale: false });
const failed = <T>(data: T | null = null): Source<T> => ({
  status: "error",
  data,
  error: "engine error (HTTP 500)",
  observedAt: data ? 1_700_000_050 : null,
  stale: data !== null,
});

const model = (over: Partial<ModelInfo>): ModelInfo => ({ status: "ready", loaded: false, ...over }) as ModelInfo;

function obs(over: Partial<Observations> = {}): Observations {
  return {
    system: ready(sysInfo()),
    metrics: { snapshot: snapshot(), status: "live", fresh: true },
    backends: ready({ backends: [backend()], ready: true, count: 1 }),
    models: ready({ models: [model({ loaded: true }), model({})] }),
    alerts: ready({ alerts: [] as Alert[] }),
    ...over,
  };
}

function node(o: Observations, id: NodeId) {
  const n = deriveNodes(o).find((x) => x.id === id);
  if (!n) throw new Error(id);
  return n.status;
}

describe("node contracts", () => {
  it("has exactly one contract per subsystem, plus the API edges", () => {
    const ids = NODE_CONTRACTS.map((c) => c.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect([...ids].sort()).toEqual(["edges", ...Object.keys(SUBSYSTEMS)].sort());
  });

  it("names engine modules that exist in this repository", () => {
    for (const c of NODE_CONTRACTS) {
      expect(c.modules.length, c.id).toBeGreaterThan(0);
      for (const m of c.modules) expect(existsSync(REPO + m), `${c.id}: ${m}`).toBe(true);
    }
  });

  it("links every node to a view, or says why there is none", () => {
    for (const c of NODE_CONTRACTS) {
      if (c.destination) expect(Object.keys(DESTINATION_LABELS)).toContain(c.destination);
      else expect(c.noDestinationReason, c.id).toBeTruthy();
    }
  });

  it("draws only known nodes, with the request path in order", () => {
    for (const e of ARCH_EDGES) {
      expect(CONTRACTS[e.from]).toBeDefined();
      expect(CONTRACTS[e.to]).toBeDefined();
    }
    const path = ARCH_EDGES.filter((e) => e.kind === "path").map((e) => `${e.from}>${e.to}`);
    expect(path).toEqual([
      "edges>gateway",
      "gateway>scheduler",
      "scheduler>router",
      "router>backend_pool",
      "backend_pool>backend",
    ]);
  });
});

describe("focus names", () => {
  it("resolves canonical ids and the documented aliases", () => {
    expect(resolveFocus("backend_pool")).toBe("backend_pool");
    expect(resolveFocus("models")).toBe("model_registry");
    expect(resolveFocus("edges")).toBe("edges");
  });

  it.each([null, undefined, "", "nope", "toString", "__proto__", "constructor"])("rejects %s", (v) => {
    expect(resolveFocus(v)).toBeNull();
  });
});

describe("status derivation", () => {
  it("never reports OK from a source that is loading or failed", () => {
    const o = obs({
      system: loading(),
      backends: failed(),
      models: loading(),
      alerts: failed(),
      metrics: { snapshot: null, status: "connecting", fresh: false },
    });
    for (const n of deriveNodes(o)) {
      expect(n.status.tone, n.id).not.toBe("ok");
    }
    expect(node(o, "backend_pool")).toMatchObject({ tone: "unknown", summary: expect.stringMatching(/^Not observed/) });
    expect(node(o, "gateway")).toMatchObject({ tone: "unknown", summary: expect.stringMatching(/^Loading/) });
    expect(node(o, "scheduler").summary).toMatch(/Waiting for live metrics/);
  });

  it("keeps independent nodes usable when one source fails", () => {
    const o = obs({ system: failed() });
    expect(node(o, "gateway").tone).toBe("unknown");
    expect(node(o, "backend_pool")).toMatchObject({ tone: "ok", summary: "1 of 1 backend available" });
    expect(node(o, "model_registry").summary).toBe("2 models · 1 loaded");
  });

  it("marks values from a failed refresh stale, with their observation time", () => {
    const o = obs({ backends: failed({ backends: [backend()], ready: true, count: 1 }) });
    expect(node(o, "backend_pool")).toMatchObject({
      tone: "ok",
      stale: true,
      provenance: [{ source: "backends", state: "stale", at: 1_700_000_050 }],
    });
  });

  it("counts the whole pool and separates availability from configuration", () => {
    const o = obs({
      backends: ready({
        backends: [backend(), backend({ name: "gpu", kind: "remote_vllm", location: "remote", available: false, in_flight: 2 })],
        ready: true,
        count: 2,
      }),
    });
    const pool = node(o, "backend_pool");
    expect(pool).toMatchObject({ tone: "attention", summary: "1 of 2 backends available" });
    expect(pool.facts).toContain("2 requests in flight");
    expect(node(o, "backend").facts[0]).toBe("Configured adapters: llamacpp (local), remote_vllm (remote)");
    expect(node(obs({ backends: ready({ backends: [], ready: false, count: 0 }) }), "backend_pool").tone).toBe("attention");
  });

  it("reports the scheduler from its own fields, without percentiles", () => {
    const s = node(obs(), "scheduler");
    expect(s).toMatchObject({
      tone: "ok",
      summary: "2/4 in use · 3 queued",
      stale: false,
      provenance: [{ source: "metrics", state: "current", at: 1_700_000_000 }],
    });
    expect(s.facts.join(" ")).toMatch(/average 12 ms, maximum 80 ms/);
    expect(s.facts.join(" ")).not.toMatch(/p\d\d|percentile/i);
    expect(node(obs({ metrics: { snapshot: snapshot(scheduler({ wait_ms_avg: null })), status: "live", fresh: true } }), "scheduler").facts).toContain(
      "Queue wait: no samples yet",
    );
    expect(node(obs({ metrics: { snapshot: snapshot(null), status: "live", fresh: true } }), "scheduler")).toMatchObject({
      tone: "unknown",
      summary: "Not reported by this engine",
    });
  });

  // R1: drain state comes from the same live snapshot as the counters.
  const live = (over: Omit<Partial<SchedulerPanel>, "draining"> & { draining?: unknown }, fresh = true) =>
    ({ snapshot: snapshot(scheduler(over)), status: fresh ? "live" : "down", fresh }) as Observations["metrics"];

  it("reports draining from the live snapshot even when the system summary says otherwise", () => {
    const o = obs({ system: ready(sysInfo({ draining: false })), metrics: live({ draining: true }) });
    expect(node(o, "scheduler")).toMatchObject({ tone: "attention", summary: "Draining · 2/4 in use · 3 queued" });
  });

  it("follows the live snapshot when an older system summary still says draining", () => {
    const o = obs({ system: ready(sysInfo({ draining: true })), metrics: live({ draining: false }) });
    expect(node(o, "scheduler")).toMatchObject({ tone: "ok", summary: "2/4 in use · 3 queued" });
  });

  it("does not need the system summary to report the scheduler", () => {
    for (const system of [loading<SystemInfo>(), failed<SystemInfo>()]) {
      expect(node(obs({ system, metrics: live({ draining: true }) }), "scheduler").tone).toBe("attention");
      expect(node(obs({ system, metrics: live({ draining: false }) }), "scheduler").tone).toBe("ok");
    }
  });

  it("treats a missing or malformed drain field as unknown, not as accepting requests", () => {
    for (const draining of [undefined, "no", 0, null]) {
      const s = node(obs({ metrics: live({ draining }) }), "scheduler");
      expect(s.tone, String(draining)).toBe("unknown");
      expect(s.summary).toBe("2/4 in use · 3 queued · drain state unknown");
    }
  });

  it("keeps a lost snapshot's drain state, marked stale", () => {
    const s = node(obs({ metrics: live({ draining: true }, false) }), "scheduler");
    expect(s).toMatchObject({ tone: "attention", stale: true });
  });

  // R2: facts from different sources keep their own freshness.
  it("marks retained adapter facts stale when the backend list fails, keeping readiness", () => {
    const o = obs({ backends: failed({ backends: [backend()], ready: true, count: 1 }) });
    const b = node(o, "backend");
    expect(b).toMatchObject({ tone: "ok", summary: "Serving tiny", stale: true });
    expect(b.facts[0]).toBe("Configured adapters: llamacpp (local) (from an earlier read; the latest failed)");
  });

  it("says adapters are not observed when the backend list never loaded", () => {
    expect(node(obs({ backends: failed() }), "backend").facts[0]).toBe(
      "Configured adapters: not observed (GET /admin/backends could not be read)",
    );
    expect(node(obs({ backends: loading() }), "backend").facts[0]).toBe("Configured adapters: loading…");
    expect(node(obs({ backends: ready({ backends: [], ready: false, count: 0 }) }), "backend").facts[0]).toBe(
      "Configured adapters: none reported",
    );
  });

  it("marks the backend stale when retained readiness is stale, even with a fresh backend list", () => {
    expect(node(obs({ system: failed(sysInfo()) }), "backend")).toMatchObject({ tone: "ok", stale: true });
    expect(node(obs(), "backend").stale).toBe(false);
  });

  it("keeps each source's own observation time for a mixed-source node", () => {
    const o = obs({
      system: ready(sysInfo(), 1_700_000_200),
      backends: ready({ backends: [backend()], ready: true, count: 1 }, 1_700_000_100),
    });
    expect(node(o, "backend").provenance).toEqual([
      { source: "system", state: "current", at: 1_700_000_200 },
      { source: "backends", state: "current", at: 1_700_000_100 },
    ]);
    // A failed adapter read marks only that source stale; readiness stays current.
    const failedList = obs({
      system: ready(sysInfo(), 1_700_000_200),
      backends: failed({ backends: [backend()], ready: true, count: 1 }),
    });
    expect(node(failedList, "backend").provenance).toEqual([
      { source: "system", state: "current", at: 1_700_000_200 },
      { source: "backends", state: "stale", at: 1_700_000_050 },
    ]);
    // Recovery clears only that source's stale state, with its new time.
    const recovered = obs({
      system: ready(sysInfo(), 1_700_000_200),
      backends: ready({ backends: [backend()], ready: true, count: 1 }, 1_700_000_300),
    });
    expect(node(recovered, "backend")).toMatchObject({
      stale: false,
      provenance: [
        { source: "system", state: "current", at: 1_700_000_200 },
        { source: "backends", state: "current", at: 1_700_000_300 },
      ],
    });
  });

  it("gives webhooks separate times for the setting and the dead-letter state", () => {
    const o = obs({ system: failed(sysInfo()), alerts: ready({ alerts: [] as Alert[] }, 1_700_000_400) });
    const w = node(o, "webhooks");
    expect(w.stale).toBe(true);
    expect(w.provenance).toEqual([
      { source: "system", state: "stale", at: 1_700_000_050 },
      { source: "alerts", state: "current", at: 1_700_000_400 },
    ]);
    expect(node(obs({ alerts: failed() }), "webhooks").provenance[1]).toEqual({ source: "alerts", state: "failed", at: null });
    expect(node(obs({ alerts: loading() }), "webhooks").provenance[1]).toEqual({ source: "alerts", state: "loading", at: null });
  });

  it("gives static nodes no provenance and never marks them stale", () => {
    const o = obs({ system: failed(sysInfo()), backends: failed(), models: failed(), alerts: failed() });
    for (const id of ["keystore", "quota", "audit", "log_buffer"] as NodeId[]) {
      expect(node(o, id)).toMatchObject({ provenance: [], stale: false });
    }
  });

  it("keeps webhook dead-letter facts when the delivery setting is not observed", () => {
    const dead: Alert = { severity: "warning", kind: "webhook_dead_letters", message: "2 webhook deliveries are dead-lettered", target_type: "webhook_deliveries", target_id: null };
    const w = node(obs({ system: failed(), alerts: ready({ alerts: [dead] }) }), "webhooks");
    expect(w.tone).toBe("unknown");
    expect(w.facts).toContain("2 webhook deliveries are dead-lettered");
  });

  // R3: missing readiness checks are unknown, not failures.
  const withChecks = (checks: Record<string, string>) => {
    const s = sysInfo();
    s.readiness.checks = checks;
    return obs({ system: ready(s) });
  };

  it.each([
    [{ database: "ok", migrations: "applied" }, "ok", "SQLite · database ok · migrations applied"],
    [{}, "unknown", "SQLite · database not reported · migrations not reported"],
    [{ database: "ok" }, "unknown", "SQLite · database ok · migrations not reported"],
    [{ migrations: "applied" }, "unknown", "SQLite · database not reported · migrations applied"],
    [{ database: "error: OperationalError", migrations: "applied" }, "attention", "SQLite · database error: OperationalError · migrations applied"],
    [{ database: "ok", migrations: "pending" }, "attention", "SQLite · database ok · migrations pending"],
    [{ database: "error: OperationalError" }, "attention", "SQLite · database error: OperationalError · migrations not reported"],
    [{ database: "fine", migrations: "applied" }, "unknown", "SQLite · database fine (unrecognized) · migrations applied"],
  ])("classifies store checks %j as %s", (checks, tone, summary) => {
    const s = node(withChecks(checks), "store");
    expect(s.tone).toBe(tone);
    expect(s.summary).toBe(summary);
  });

  it("explains which store check is missing next to a known failure", () => {
    const s = node(withChecks({ database: "error: OperationalError" }), "store");
    expect(s.facts).toContain("The database check failed: error: OperationalError");
    expect(s.facts).toContain("The migrations check was not reported, so its state is unknown");
  });

  it("does not present a lost metrics connection as current", () => {
    const lost = obs({ metrics: { snapshot: snapshot(), status: "down", fresh: false } });
    expect(node(lost, "scheduler").stale).toBe(true);
    expect(node(lost, "telemetry")).toMatchObject({ tone: "unknown", stale: true });
    expect(node(obs({ metrics: { snapshot: snapshot(), status: "polling", fresh: true } }), "telemetry")).toMatchObject({
      tone: "ok",
      summary: "Polling /metrics (stream unavailable)",
    });
  });

  it("treats disabled webhooks as off, and needs a successful alerts read for 'none dead-lettered'", () => {
    const dead: Alert = { severity: "warning", kind: "webhook_dead_letters", message: "3 webhook deliveries are dead-lettered", target_type: "webhook_deliveries", target_id: null };
    expect(node(obs({ system: ready(sysInfo({}, { webhooks_enabled: false })) }), "webhooks")).toMatchObject({
      tone: "off",
      summary: "Delivery off (webhooks_enabled=false)",
      facts: ["No dead-lettered deliveries"],
    });
    expect(node(obs({ alerts: failed() }), "webhooks")).toMatchObject({ tone: "unknown", summary: "Delivery on · dead letters not observed" });
    expect(node(obs({ alerts: loading() }), "webhooks").tone).toBe("unknown");
    expect(node(obs({ alerts: ready({ alerts: [dead] }) }), "webhooks")).toMatchObject({
      tone: "attention",
      summary: "3 webhook deliveries are dead-lettered",
    });
    expect(node(obs(), "webhooks")).toMatchObject({ tone: "ok", summary: "Delivery on · none dead-lettered" });
  });

  it("never claims the audit chain was verified", () => {
    expect(node(obs(), "audit")).toMatchObject({ tone: "unknown", summary: "Chain not checked here" });
  });

  it("uses the readiness probe's own checks for the store", () => {
    expect(node(obs(), "store").tone).toBe("ok");
    const pending = sysInfo();
    pending.readiness.checks = { database: "ok", migrations: "pending" };
    expect(node(obs({ system: ready(pending) }), "store")).toMatchObject({ tone: "attention" });
    const missing = sysInfo();
    missing.readiness.checks = {};
    expect(node(obs({ system: ready(missing) }), "store").summary).toBe(
      "SQLite · database not reported · migrations not reported",
    );
  });

  it("shows switched-off features as off, and configuration as configuration", () => {
    const o = obs({ system: ready(sysInfo({}, { allow_model_management: false, client_events_enabled: false, grpc_enabled: false })) });
    expect(node(o, "model_service").tone).toBe("off");
    expect(node(o, "client_events").tone).toBe("off");
    expect(node(o, "edges")).toMatchObject({ tone: "info", summary: "HTTP only (gRPC off)" });
    expect(node(obs({ system: loading() }), "edges").facts).toContain("gRPC: unknown");
    expect(node(o, "gateway").tone).toBe("info");
  });

  it("says when a backend cannot serve, and why", () => {
    const off = sysInfo();
    off.readiness.inference = { available: false, reason: "no model loaded" };
    expect(node(obs({ system: ready(off) }), "backend")).toMatchObject({
      tone: "attention",
      summary: "Not serving: no model loaded",
    });
    expect(node(obs(), "backend")).toMatchObject({ tone: "ok", summary: "Serving tiny" });
  });

  it("reports an empty registry as empty, not as unknown", () => {
    expect(node(obs({ models: ready({ models: [] }) }), "model_registry")).toMatchObject({
      tone: "info",
      summary: "No models registered",
    });
  });
});
