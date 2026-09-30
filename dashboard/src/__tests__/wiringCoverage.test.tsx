import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { JSX } from "react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../hooks/useLiveMetrics", () => ({
  useLiveMetrics: () => ({ snapshot: null, status: "live" }),
}));
vi.mock("../hooks/useLiveFeed", () => ({
  useLiveFeed: () => ({
    events: [{ type: "request.end", ts: 1_700_000_000, request_id: "req-1", model: "tiny" }],
    status: "live",
  }),
}));

import { App } from "../App";
import { Overview } from "../views/Overview";
import { Monitoring } from "../views/Monitoring";
import { Clients } from "../views/Clients";
import { Models } from "../views/Models";
import { Logs } from "../views/Logs";
import { Security } from "../views/Security";
import { Keys } from "../views/Keys";
import { System } from "../views/System";
import { Routing } from "../views/Routing";
import { Architecture } from "../views/Architecture";
import { api, ApiError, setApiKey } from "../lib/api";
import { LOCAL_CONTROLS, WIRING, explain } from "../lib/wiring";
import { SystemProvider } from "../hooks/useSystem";

// Coverage of the dashboard's controls by the wiring registry.
//
// Each case renders a surface in a specific state (populated, error, empty, a
// confirmation dialog, a sign-in gate) and then requires, for every control:
//  1. it sits inside a [data-wiring] scope whose ids resolve in the registry;
//  2. each scope id is explained by a visible "How this works" button on the
//     same active surface. Inside a dialog only a button in that dialog counts;
//     a matching button behind the modal does not.
// Finally, every registry entry must be explained somewhere.
//
// Controls: button, a[href], input, select, textarea, [role=tab], and the
// clickable client rows (tr.clickrow). Exclusions, by design: the "How this
// works" buttons themselves and anything inside an open popover (its docs
// links), so help is never required for help. A clickable row counts as
// pointer-only unless it contains an enabled native button that does the same
// thing (its keyboard equivalent); the audit reports any it finds.

const CONTROLS = 'button, a[href], input, select, textarea, [role="tab"], tr.clickrow';

const model = (over: Record<string, unknown>) => ({
  id: "m",
  name: "m",
  filename: "m.gguf",
  source_type: "url",
  source_ref: "http://x/m.gguf",
  sha256: "abc",
  size_bytes: 1000,
  downloaded_bytes: 1000,
  progress: 1,
  quant: "Q4_K_M",
  arch: "llama",
  context_length: 4096,
  status: "ready",
  error: null,
  active: false,
  loaded: false,
  added_at: "2026-09-20T00:00:00+00:00",
  compat: { status: "ok", reason: "should run" },
  ...over,
});

const key = (over: Record<string, unknown>) => ({
  id: "k1",
  prefix: "sk-ie-ab12",
  label: "app",
  created_at: "2026-09-20T00:00:00+00:00",
  last_used_at: null,
  revoked: false,
  role: "operator",
  client_id: null,
  ...over,
});

const ok = <T,>(v: T) => Promise.resolve(v as never);
const boom = () => Promise.reject(new ApiError("engine error", 500));

const systemInfo = (switches: Record<string, boolean> = {}) => ({
  build: { version: "0.2.0", commit: null, built_at: null },
  readiness: { ready: true, checks: {}, inference: { available: false } },
  draining: false,
  switches: {
    allow_model_management: true,
    allow_network_downloads: true,
    allow_structured_output: true,
    diagnostics_enabled: true,
    require_auth: true,
    webhooks_enabled: false,
    client_events_enabled: true,
    ip_allowlist_set: false,
    grpc_enabled: false,
    ...switches,
  },
  metadata: { grpc_port: null, billing_provider: null },
  routing: {
    backend_kind: "llama_cpp", virtual_models: [], workload_routing_enabled: false,
    workload_rule_count: 0, remote_workers: [], spillover_providers: [],
  },
});

function mockApi() {
  vi.spyOn(api, "system").mockImplementation(() => ok(systemInfo()));
  vi.spyOn(api, "diagnostics").mockImplementation(() => ok({}));
  vi.spyOn(api, "backends").mockImplementation(() =>
    ok({
      backends: [
        {
          name: "primary", kind: "llamacpp", location: "local", state: "ready", available: true,
          in_flight: 0, max_in_flight: 1, model_id: "tiny", served_model: null, context_length: 2048,
          supports_prefix_cache: false, supports_kv_cache_metrics: false, tier: "primary", external: false,
        },
      ],
      ready: true,
      count: 1,
    }),
  );
  vi.spyOn(api, "routes").mockImplementation(() =>
    ok({
      routes: [
        {
          model: "tiny", backend: "primary", requests: 1, errors: 0, cancelled: 0, prompt_tokens: 3,
          completion_tokens: 2, cost: 0, success_rate: 1, avg_total_ms: 10, avg_ttft_ms: 4,
          avg_queue_wait_ms: 0, avg_output_tps: 200, upstream_attempts: 0, tier: "primary",
          reasons: { model_map: 1 }, fallbacks: {}, policies: { base: 1 }, workload_rules: {},
        },
      ],
      sheds: {},
      totals: { requests: 1, errors: 0, cancelled: 0, cost: 0, sheds: 0 },
    }),
  );
  vi.spyOn(api, "routePlan").mockImplementation(() =>
    ok({
      model: "tiny", policy: "base", known: true, chosen: "primary", chosen_step: 0,
      steps: [{ targets: ["*all*"], chosen: "primary", candidates: [{ name: "primary", state: "ready", available: true, in_flight: 0, has_capacity: true }] }],
      workload_rule: null, workload_rule_preferred_targets: null,
    }),
  );
  vi.spyOn(api, "identity").mockImplementation(() =>
    ok({ kind: "key", auth_required: true, key: { id: "k1", prefix: "sk-ie-ab12", label: "owner", role: "operator" } }),
  );
  vi.spyOn(api, "overview").mockImplementation(() => ok({ version: "0.2.0", ready: true, checks: {} }));
  vi.spyOn(api, "alerts").mockImplementation(() =>
    ok({
      alerts: [
        { severity: "warning", kind: "client_suspended", message: "Client suspended", target_type: "client", target_id: "c1" },
        { severity: "critical", kind: "dead_deliveries", message: "Dead deliveries", target_type: "webhook_deliveries", target_id: null },
      ],
    }),
  );
  const attribution = {
    window: "5h",
    since: 0,
    keys: [{
      key_id: "k1", key_prefix: "sk-ie-ab12", key_label: "app", client_id: "c1",
      requests: 5, prompt_tokens: 10, completion_tokens: 5, cu: 42, errors: 1, last_ts: 1_700_000_000,
    }],
  };
  vi.spyOn(api, "usageAttribution").mockImplementation(() => ok(attribution));
  vi.spyOn(api, "errorTaxonomy").mockImplementation(() =>
    ok({ window: "5h", since: 0, categories: { backend: 2 }, total: 2 }),
  );
  vi.spyOn(api, "listClients").mockImplementation(() =>
    ok({ clients: [{ id: "c1", external_ref: "cus_1", email: "a@x.io", status: "active" }] }),
  );
  vi.spyOn(api, "listWebhookEndpoints").mockImplementation(() =>
    ok({ endpoints: [{ id: "e1", url: "https://hooks.example/x", event_types: null, disabled: false }] }),
  );
  vi.spyOn(api, "listDeliveries").mockImplementation(() =>
    ok({
      deliveries: [{
        id: "d1", endpoint_id: "e1", event_type: "invoice.paid", status: "succeeded",
        attempts: 1, last_status_code: 200, last_error: null, created_at: 1_700_000_000,
      }],
    }),
  );
  const plan = {
    id: "pro", name: "Pro", quota_5h_cu: 100, quota_weekly_cu: 1000, rate_limit_per_min: 60, allowed_models: null,
  };
  vi.spyOn(api, "listPlans").mockImplementation(() => ok({ plans: [plan] }));
  vi.spyOn(api, "upsertPlan").mockImplementation(() => ok(plan));
  vi.spyOn(api, "createClient").mockImplementation(() =>
    ok({ id: "c2", external_ref: null, email: null, status: "active" }),
  );
  vi.spyOn(api, "createClientKey").mockImplementation(() =>
    ok({ id: "k9", prefix: "sk-ie-zz99", label: null, client_id: "c1", token: "sk-ie-zz99-one-time" }),
  );
  vi.spyOn(api, "reconcileClient").mockImplementation(() => ok({ client_id: "c1", cu_5h: 4, cu_week: 42 }));
  vi.spyOn(api, "createWebhookEndpoint").mockImplementation(() =>
    ok({ id: "e2", client_id: "c1", url: "https://hooks.example/y", description: null, disabled: false, event_types: null, secret: "whsec_new" }),
  );
  vi.spyOn(api, "setEndpointDisabled").mockImplementation(() => ok({ id: "e1", disabled: true }));
  vi.spyOn(api, "rotateEndpointSecret").mockImplementation(() =>
    ok({ id: "e1", secret: "whsec_rotated", grace_s: 86_400, previous_secret_expires_at: 1_700_086_400 }),
  );
  vi.spyOn(api, "deleteEndpoint").mockImplementation(() => ok({ id: "e1", deleted: true }));
  vi.spyOn(api, "replayDelivery").mockImplementation(() => ok({ id: "d1", status: "pending" }));
  vi.spyOn(api, "listModels").mockImplementation(() =>
    ok({
      models: [
        model({ id: "m1", name: "downloading", status: "downloading", progress: 0.5, downloaded_bytes: 500 }),
        model({ id: "m2", name: "ready" }),
        model({ id: "m3", name: "loaded", loaded: true, active: true }),
      ],
    }),
  );
  vi.spyOn(api, "getModel").mockImplementation((id: string) => ok(model({ id, name: "ready" })));
  vi.spyOn(api, "logs").mockImplementation(() =>
    ok({ events: [{ ts: 1_700_000_000, level: "INFO", event: "request.end", request_id: "req-1" }] }),
  );
  vi.spyOn(api, "audit").mockImplementation(() =>
    ok({ events: [{ id: 1, ts: 1_700_000_000, actor: "k1", action: "key.create", target: "k2", detail: null, hash: "abcdef0123456789" }] }),
  );
  vi.spyOn(api, "listKeys").mockImplementation(() =>
    ok({ keys: [key({ id: "k1" }), key({ id: "k2", prefix: "sk-ie-cd34", revoked: true })] }),
  );
  vi.spyOn(api, "createKey").mockImplementation(() =>
    ok({ id: "k3", prefix: "sk-ie-ef56", label: null, token: "sk-ie-ef56-test-token" }),
  );
}

interface Report {
  surface: string;
  controls: number;
  pointerOnly: number;
  explained: string[];
}

const reports: Report[] = [];

function idsOf(el: Element, attr: string): string[] {
  return (el.getAttribute(attr) ?? "").split(" ").filter(Boolean);
}

// A row's keyboard equivalent: an enabled native button inside it, in the same
// wiring scope as the row, with an accessible name.
function keyboardEquivalent(row: HTMLElement): HTMLButtonElement | null {
  const button = row.querySelector<HTMLButtonElement>("button.rowbtn");
  if (!button || button.disabled || button.tabIndex < 0) return null;
  if (button.closest("[data-wiring]") !== row) return null;
  if (!(button.getAttribute("aria-label") ?? button.textContent ?? "").trim()) return null;
  return button;
}

// Audits one active surface: the open modal dialog if there is one, else `root`.
function audit(surface: string, root: HTMLElement = document.body): Report {
  const dialog = root.querySelector<HTMLElement>('[role="dialog"][aria-modal="true"]');
  const active = dialog ?? root;
  const controls = Array.from(active.querySelectorAll<HTMLElement>(CONTROLS)).filter(
    (el) => !el.classList.contains("wired-btn") && !el.closest(".wired-pop"),
  );

  // Visible hint buttons on this surface, by the ids they explain.
  const hints = new Map<string, HTMLElement[]>();
  active.querySelectorAll<HTMLElement>("[data-wiring-ids]").forEach((w) => {
    const btn = w.querySelector<HTMLElement>(".wired-btn");
    if (!btn) return;
    for (const id of idsOf(w, "data-wiring-ids")) hints.set(id, [...(hints.get(id) ?? []), btn]);
  });

  const problems: string[] = [];
  for (const el of controls) {
    const name = el.getAttribute("aria-label") ?? el.textContent?.trim() ?? el.tagName;
    const scope = el.closest("[data-wiring]");
    if (!scope || !active.contains(scope)) {
      problems.push(`${name}: no wiring scope on this surface`);
      continue;
    }
    for (const id of idsOf(scope, "data-wiring")) {
      expect(() => explain(id), `${surface}: ${id}`).not.toThrow();
      const buttons = hints.get(id) ?? [];
      const visible = buttons.find((b) => {
        try {
          expect(b).toBeVisible();
          expect(b.getAttribute("aria-label") ?? "").toMatch(/^How this works: /);
          return true;
        } catch {
          return false;
        }
      });
      if (!visible) problems.push(`${name}: "${id}" has no visible hint on this surface`);
    }
  }
  expect(problems, surface).toEqual([]);

  const report = {
    surface,
    controls: controls.length,
    pointerOnly: controls.filter((el) => el.matches("tr.clickrow") && !keyboardEquivalent(el)).length,
    explained: [...hints.keys()].sort(),
  };
  reports.push(report);
  return report;
}

async function show(ui: JSX.Element, ready: () => Promise<unknown>) {
  render(ui);
  await ready();
}

beforeEach(() => {
  window.location.hash = "";
  setApiKey(null);
  mockApi();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

afterAll(() => {
  // Printed to the CI log as the per-surface coverage evidence.
  const lines = reports.map(
    (r) =>
      `${r.surface.padEnd(34)} controls=${String(r.controls).padStart(2)}` +
      `${r.pointerOnly ? ` (pointer-only rows=${r.pointerOnly})` : ""}  ids=${r.explained.join(", ")}`,
  );
  console.info(["Wiring coverage by surface and state:", ...lines].join("\n"));
});

describe("wiring coverage of dashboard controls", () => {
  it("Overview (stat cards, readiness, resources, feed)", async () => {
    await show(<Overview />, () => screen.findByTestId("engine-readiness"));
    for (const card of ["Requests", "Tokens", "Uptime", "Energy"]) {
      expect(screen.getByRole("button", { name: `How this works: ${card} card` })).toBeVisible();
    }
    const r = audit("Overview");
    expect(r.explained).toEqual(
      expect.arrayContaining(["overview.metrics", "overview.metrics-fallback", "overview.readiness", "overview.feed"]),
    );
  });

  it("Monitoring: populated", async () => {
    await show(<Monitoring onNavigate={vi.fn()} />, async () => {
      await screen.findByText("Client suspended");
      await screen.findByText("sk-ie-ab12");
    });
    audit("Monitoring: populated");
  });

  it("Monitoring: errors with retry", async () => {
    vi.mocked(api.alerts).mockImplementation(boom);
    vi.mocked(api.usageAttribution).mockImplementation(boom);
    vi.mocked(api.errorTaxonomy).mockImplementation(boom);
    await show(<Monitoring onNavigate={vi.fn()} />, async () => {
      expect((await screen.findAllByRole("button", { name: "Retry" })).length).toBe(3);
    });
    audit("Monitoring: errors");
  });

  it("Clients: populated, client open", async () => {
    await show(<Clients focusClientId="c1" onNavigate={vi.fn()} />, async () => {
      await screen.findByText("https://hooks.example/x");
      await screen.findByText("invoice.paid");
    });
    const r = audit("Clients: populated");
    // Populated rows exist, and every one is usable from the keyboard.
    const rows = Array.from(document.querySelectorAll<HTMLElement>("tr.clickrow"));
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) expect(keyboardEquivalent(row)).not.toBeNull();
    expect(r.pointerOnly).toBe(0);
  });

  it("Clients: plans tab, edit, and the update dialog", async () => {
    await show(<Clients onNavigate={vi.fn()} />, () => screen.findByText("a@x.io"));
    await userEvent.click(screen.getByRole("tab", { name: "Plans" }));
    await screen.findByText("Pro");
    audit("Clients: plans tab");
    await userEvent.click(screen.getByRole("button", { name: "Edit plan pro" }));
    audit("Clients: plans tab, editing");
    await userEvent.click(screen.getByRole("button", { name: /^Update plan/ }));
    expect(audit("Clients: plan update dialog").explained).toEqual(["local.dialog-cancel", "plans.upsert"]);
  });

  it("Clients: plan model list, the create dialog, and a failed plan list", async () => {
    await show(<Clients onNavigate={vi.fn()} />, () => screen.findByText("a@x.io"));
    await userEvent.click(screen.getByRole("tab", { name: "Plans" }));
    await screen.findByText("Pro");
    await userEvent.click(screen.getByRole("radio", { name: "Only these models" }));
    audit("Clients: plans tab, model list");
    await userEvent.type(screen.getByLabelText("Plan id"), "team");
    await userEvent.type(screen.getByLabelText("Name"), "Team");
    await userEvent.type(screen.getByLabelText("Model names"), "tiny");
    await userEvent.click(screen.getByRole("button", { name: /^Create plan/ }));
    expect(audit("Clients: plan create dialog").explained).toEqual(["local.dialog-cancel", "plans.upsert"]);
  });

  it("Clients: a failed plan list pauses saving and offers a retry", async () => {
    vi.mocked(api.listPlans).mockRejectedValue(new ApiError("engine unavailable", 503, "unavailable"));
    await show(<Clients onNavigate={vi.fn()} />, () => screen.findByText("a@x.io"));
    await userEvent.click(screen.getByRole("tab", { name: "Plans" }));
    await screen.findByRole("button", { name: "Retry loading plans" });
    audit("Clients: plan list failed");
  });

  it("Clients: one-time client key, endpoint secret, and rotated secret", async () => {
    await show(<Clients focusClientId="c1" onNavigate={vi.fn()} />, () => screen.findByText("invoice.paid"));
    await userEvent.click(screen.getByRole("button", { name: /^Create client key/ }));
    await screen.findByTestId("new-client-token");
    await userEvent.click(screen.getByRole("button", { name: /^Reconcile usage/ }));
    await screen.findByTestId("reconcile-result");
    audit("Clients: new client key + reconcile");
    await userEvent.type(screen.getByLabelText("URL"), "https://hooks.example/y");
    await userEvent.click(screen.getByRole("button", { name: /^Add endpoint/ }));
    await screen.findByTestId("new-endpoint-secret");
    audit("Clients: new endpoint secret");
    await userEvent.click(screen.getByRole("button", { name: "Rotate secret for endpoint https://hooks.example/x" }));
    expect(audit("Clients: rotate dialog").explained).toEqual(["local.dialog-cancel", "webhooks.rotate-secret"]);
    await userEvent.click(screen.getByRole("button", { name: "Rotate secret" }));
    await screen.findByTestId("rotated-secret");
    audit("Clients: rotated secret");
  });

  it("Clients: endpoint disable, delete, and delivery replay dialogs", async () => {
    await show(<Clients focusClientId="c1" onNavigate={vi.fn()} />, () => screen.findByText("invoice.paid"));
    await userEvent.click(screen.getByRole("button", { name: "Disable endpoint https://hooks.example/x" }));
    expect(audit("Clients: disable dialog").explained).toEqual(["local.dialog-cancel", "webhooks.set-disabled"]);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete endpoint https://hooks.example/x" }));
    expect(audit("Clients: delete dialog").explained).toEqual(["local.dialog-cancel", "webhooks.delete"]);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: /^Replay invoice\.paid delivery/ }));
    expect(audit("Clients: replay dialog").explained).toEqual(["local.dialog-cancel", "webhooks.replay"]);
  });

  it("Clients: error with retry", async () => {
    vi.mocked(api.listClients).mockImplementation(boom);
    await show(<Clients onNavigate={vi.fn()} />, () => screen.findByRole("button", { name: "Retry" }));
    audit("Clients: error");
  });

  it("Models: downloading, ready, and loaded rows", async () => {
    await show(<Models />, () => screen.findByText("loaded", { selector: ".mono" }));
    audit("Models: populated");
  });

  it("Models: add form in each source mode", async () => {
    await show(<Models />, () => screen.findByText("ready", { selector: ".mono" }));
    for (const tab of ["Hugging Face", "URL", "Local file"]) {
      await userEvent.click(screen.getByRole("tab", { name: tab }));
      audit(`Models: add form (${tab})`);
    }
  });

  it("Models: delete confirmation dialog", async () => {
    await show(<Models />, () => screen.findByText("ready", { selector: ".mono" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete ready" }));
    const dialog = screen.getByRole("dialog", { name: "Delete ready?" });
    const r = audit("Models: delete dialog");
    expect(r.explained).toEqual(["local.dialog-cancel", "models.delete"]);
    // The hint opens inside the dialog and explains the real call.
    await userEvent.click(within(dialog).getByRole("button", { name: "How this works: Delete model" }));
    expect(within(dialog).getByRole("group")).toHaveTextContent("DELETE /admin/models/{model_id}");
  });

  it("Models: details drawer (ready, error, not found, loading)", async () => {
    await show(<Models modelId="m2" onNavigate={vi.fn()} />, () => screen.findByTestId("model-details"));
    const ready = audit("Models: details drawer");
    expect(ready.explained).toEqual(["local.close-drawer", "local.copy-checksum", "models.detail"]);
    cleanup();
    vi.mocked(api.getModel).mockImplementation(boom);
    await show(<Models modelId="m2" onNavigate={vi.fn()} />, () => screen.findByRole("button", { name: "Retry" }));
    audit("Models: details drawer error");
    cleanup();
    vi.mocked(api.getModel).mockImplementation(() => Promise.reject(new ApiError("gone", 404, "model_not_found")));
    await show(<Models modelId="gone" onNavigate={vi.fn()} />, () => screen.findByText(/no longer exists/));
    audit("Models: details drawer not found");
    cleanup();
    vi.mocked(api.getModel).mockImplementation(() => new Promise(() => {}));
    await show(<Models modelId="m2" onNavigate={vi.fn()} />, () => screen.findByRole("dialog"));
    audit("Models: details drawer loading");
  });

  it("Models: empty and error", async () => {
    vi.mocked(api.listModels).mockImplementation(() => ok({ models: [] }));
    await show(<Models />, () => screen.findByText(/No models yet/));
    audit("Models: empty");
    cleanup();
    vi.mocked(api.listModels).mockImplementation(boom);
    await show(<Models />, () => screen.findByRole("button", { name: "Retry" }));
    audit("Models: error");
  });

  it("Models: management switch off (disabled actions and their notice)", async () => {
    vi.mocked(api.system).mockImplementation(() =>
      ok(systemInfo({ allow_model_management: false, allow_network_downloads: false })),
    );
    await show(
      <SystemProvider>
        <Models />
      </SystemProvider>,
      () => screen.findByText(/Loading, unloading and deleting/),
    );
    expect(screen.getByRole("button", { name: "Delete ready" })).toBeDisabled();
    const r = audit("Models: switches off");
    expect(r.explained).toContain("app.system");
  });

  it("Models: switch status unavailable, with retry", async () => {
    vi.mocked(api.system).mockImplementation(boom);
    await show(
      <SystemProvider>
        <Models />
      </SystemProvider>,
      () => screen.findByText(/Feature-switch status unavailable/),
    );
    const r = audit("Models: switch status unavailable");
    expect(r.explained).toContain("app.system");
  });

  it("System: populated, with the diagnostics download", async () => {
    await show(
      <SystemProvider>
        <System />
      </SystemProvider>,
      () => screen.findByRole("heading", { name: "Feature switches" }),
    );
    const r = audit("System: populated");
    expect(r.explained).toEqual(expect.arrayContaining(["app.system", "system.diagnostics"]));
  });

  it("System: diagnostics switch off", async () => {
    vi.mocked(api.system).mockImplementation(() => ok(systemInfo({ diagnostics_enabled: false })));
    await show(
      <SystemProvider>
        <System />
      </SystemProvider>,
      () => screen.findByText(/diagnostics_enabled=false/),
    );
    audit("System: diagnostics off");
  });

  it("System: error with retry", async () => {
    vi.mocked(api.system).mockImplementation(boom);
    await show(
      <SystemProvider>
        <System />
      </SystemProvider>,
      () => screen.findByRole("button", { name: "Retry" }),
    );
    audit("System: error");
  });

  it("Architecture: diagram, a selected node's details, and the text list", async () => {
    await show(
      <SystemProvider>
        <Architecture focus="backend_pool" onSelect={vi.fn()} />
      </SystemProvider>,
      () => screen.findAllByText(/1 of 1 backend available/),
    );
    const r = audit("Architecture: node selected");
    expect(r.explained).toEqual(
      expect.arrayContaining([
        "app.system",
        "routing.backends",
        "models.list",
        "monitoring.alerts",
        "overview.metrics",
        "local.architecture-node",
        "local.architecture-open",
      ]),
    );
  });

  it("Routing: populated, with a dry-run result", async () => {
    await show(
      <SystemProvider>
        <Routing />
      </SystemProvider>,
      () => screen.findByRole("region", { name: "Per-route table" }),
    );
    await userEvent.type(screen.getByLabelText("Model name"), "tiny{Enter}");
    await screen.findByTestId("route-plan-result");
    const r = audit("Routing: populated + dry run");
    expect(r.explained).toEqual(
      expect.arrayContaining(["routing.backends", "routing.routes", "routing.plan", "app.system", "local.docs-link"]),
    );
  });

  it("Routing: errors with retry", async () => {
    vi.mocked(api.backends).mockImplementation(boom);
    vi.mocked(api.routes).mockImplementation(boom);
    vi.mocked(api.system).mockImplementation(boom);
    await show(
      <SystemProvider>
        <Routing />
      </SystemProvider>,
      async () => {
        expect((await screen.findAllByRole("button", { name: "Retry" })).length).toBe(3);
      },
    );
    audit("Routing: errors");
  });

  it("Logs: filtered to a request", async () => {
    await show(<Logs requestId="req-1" onNavigate={vi.fn()} />, () => screen.findByText("Clear request filter"));
    audit("Logs: request filter");
  });

  it("Logs: error with retry", async () => {
    vi.mocked(api.logs).mockImplementation(boom);
    await show(<Logs />, () => screen.findByRole("button", { name: "Retry" }));
    audit("Logs: error");
  });

  it("Security: populated and error", async () => {
    await show(<Security />, () => screen.findByText("key.create"));
    audit("Security: populated");
    cleanup();
    vi.mocked(api.audit).mockImplementation(boom);
    await show(<Security />, () => screen.findByRole("button", { name: "Retry" }));
    audit("Security: error");
  });

  it("Keys: active, revoked, and a new token", async () => {
    await show(<Keys />, () => screen.findByText("sk-ie-cd34…"));
    await userEvent.click(screen.getByRole("button", { name: /^Create key/ }));
    await screen.findByTestId("new-token");
    audit("Keys: populated + new token");
  });

  it("Keys: revoke and delete confirmation dialogs", async () => {
    await show(<Keys />, () => screen.findByText("sk-ie-cd34…"));
    await userEvent.click(screen.getByRole("button", { name: "Revoke key sk-ie-ab12" }));
    expect(audit("Keys: revoke dialog").explained).toEqual(["keys.revoke", "local.dialog-cancel"]);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete key sk-ie-cd34" }));
    expect(audit("Keys: delete dialog").explained).toEqual(["keys.delete", "local.dialog-cancel"]);
  });

  it("Keys: error with retry", async () => {
    vi.mocked(api.listKeys).mockImplementation(boom);
    await show(<Keys />, () => screen.findByRole("button", { name: "Retry" }));
    audit("Keys: error");
  });

  it("App shell: navigation, identity, change-key dialog", async () => {
    window.location.hash = "#/does-not-exist";
    await show(<App />, () => screen.findByRole("heading", { name: "Page not available" }));
    audit("App shell (not-found view)");
    await userEvent.click(screen.getByRole("button", { name: "Change key" }));
    const r = audit("App shell: change-key dialog");
    expect(r.explained).toEqual(["app.identity", "local.dialog-cancel", "local.saved-key"]);
    // Initial focus stays on the key field, not on the hint.
    expect(screen.getByLabelText("Operator API key")).toHaveFocus();
  });

  it("App shell: Show wiring on", async () => {
    await show(<App />, () => screen.findByRole("button", { name: "Show wiring" }));
    await userEvent.click(screen.getByRole("button", { name: "Show wiring" }));
    expect(screen.getAllByTestId("wiring-chips").length).toBeGreaterThan(0);
    expect(audit("App shell: Show wiring on").explained).toContain("local.show-wiring");
    await userEvent.click(screen.getByRole("button", { name: "Show wiring" }));
  });

  it("Gate: sign-in (401)", async () => {
    vi.mocked(api.identity).mockRejectedValue(new ApiError("operator access required", 401));
    await show(<App />, () => screen.findByLabelText("Operator API key"));
    audit("Gate: sign-in");
  });

  it("Gate: operator role required (403)", async () => {
    vi.mocked(api.identity).mockRejectedValue(new ApiError("client key", 403, "operator_role_required"));
    await show(<App />, () => screen.findByRole("button", { name: "Forget saved key" }));
    audit("Gate: operator role required");
  });

  it("Gate: engine unavailable, with retry", async () => {
    vi.mocked(api.identity).mockRejectedValue(new ApiError("network error: down", 0));
    await show(<App />, () => screen.findByRole("heading", { name: "Engine unavailable" }));
    audit("Gate: engine unavailable");
  });

  it("explains every registry entry somewhere in the dashboard", () => {
    const shown = new Set(reports.flatMap((r) => r.explained));
    const all = [...WIRING.map((w) => w.id), ...LOCAL_CONTROLS.map((c) => c.id)];
    expect(all.filter((id) => !shown.has(id))).toEqual([]);
  });
});
