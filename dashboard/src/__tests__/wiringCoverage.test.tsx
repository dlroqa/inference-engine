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
// links), so help is never required for help. Client rows are pointer-only (not
// keyboard-focusable); the audit counts and reports them as a known limitation.

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
  vi.spyOn(api, "listModels").mockImplementation(() =>
    ok({
      models: [
        model({ id: "m1", name: "downloading", status: "downloading", progress: 0.5, downloaded_bytes: 500 }),
        model({ id: "m2", name: "ready" }),
        model({ id: "m3", name: "loaded", loaded: true, active: true }),
      ],
    }),
  );
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
    pointerOnly: controls.filter((el) => el.matches("tr.clickrow")).length,
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
    expect(r.pointerOnly).toBe(1);
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
