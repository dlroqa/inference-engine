import { cleanup, render, screen, waitFor } from "@testing-library/react";
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

// Coverage of the dashboard's controls by the wiring registry. Every view is
// rendered with data that makes all of its conditional controls appear, then:
//  1. every interactive control sits inside a [data-wiring] scope;
//  2. every id in a scope resolves in the registry (a backend call or a local one);
//  3. every scope's ids are explained by a visible "How this works" button there;
//  4. across the dashboard, every registry entry is shown somewhere.

const CONTROLS = 'button, a[href], input, select, textarea, [role="tab"]';

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

function mockApi() {
  const ok = <T,>(v: T) => Promise.resolve(v as never);
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
  scoped: string[];
  explained: string[];
}

const reports: Report[] = [];

function audit(surface: string, root: HTMLElement): Report {
  const controls = Array.from(root.querySelectorAll<HTMLElement>(CONTROLS)).filter(
    (el) => !el.classList.contains("wired-btn"),
  );
  const unscoped = controls
    .filter((el) => !el.closest("[data-wiring]"))
    .map((el) => el.getAttribute("aria-label") ?? el.textContent?.trim() ?? el.outerHTML.slice(0, 60));
  expect(unscoped, `${surface}: controls with no wiring scope`).toEqual([]);

  const scoped = new Set<string>();
  root.querySelectorAll("[data-wiring]").forEach((el) => {
    for (const id of el.getAttribute("data-wiring")!.split(" ")) scoped.add(id);
  });
  const explained = new Set<string>();
  root.querySelectorAll("[data-wiring-ids]").forEach((el) => {
    for (const id of el.getAttribute("data-wiring-ids")!.split(" ")) explained.add(id);
  });
  for (const id of scoped) expect(() => explain(id), `${surface}: ${id}`).not.toThrow();
  const unexplained = [...scoped].filter((id) => !explained.has(id));
  expect(unexplained, `${surface}: scopes without a "How this works" button`).toEqual([]);

  const report = { surface, controls: controls.length, scoped: [...scoped].sort(), explained: [...explained].sort() };
  reports.push(report);
  return report;
}

async function renderView(surface: string, ui: JSX.Element, ready: () => Promise<unknown>) {
  const { container } = render(ui);
  await ready();
  return audit(surface, container);
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
    (r) => `${r.surface.padEnd(22)} controls=${String(r.controls).padStart(2)}  ids=${r.explained.join(", ")}`,
  );
  console.info(["Wiring coverage by surface:", ...lines].join("\n"));
});

describe("wiring coverage of dashboard controls", () => {
  it("Overview", async () => {
    await renderView("Overview", <Overview />, () => screen.findByTestId("engine-readiness"));
  });

  it("Monitoring", async () => {
    await renderView("Monitoring", <Monitoring onNavigate={vi.fn()} />, async () => {
      await screen.findByText("Client suspended");
      await screen.findByText("sk-ie-ab12");
    });
  });

  it("Clients (with a client open)", async () => {
    await renderView("Clients", <Clients focusClientId="c1" onNavigate={vi.fn()} />, async () => {
      await screen.findByText("https://hooks.example/x");
      await screen.findByText("invoice.paid");
    });
  });

  it("Models (downloading, ready, and loaded rows)", async () => {
    await renderView("Models", <Models />, () => screen.findByText("loaded", { selector: ".mono" }));
  });

  it("Models add form in each source mode", async () => {
    render(<Models />);
    await screen.findByText("ready", { selector: ".mono" });
    for (const tab of ["Hugging Face", "URL", "Local file"]) {
      await userEvent.click(screen.getByRole("tab", { name: tab }));
      audit(`Models (${tab})`, document.body);
    }
  });

  it("Logs (filtered to a request)", async () => {
    await renderView("Logs", <Logs requestId="req-1" onNavigate={vi.fn()} />, () =>
      screen.findByText("Clear request filter"),
    );
  });

  it("Security", async () => {
    await renderView("Security", <Security />, () => screen.findByText("key.create"));
  });

  it("Keys (active, revoked, and a new token)", async () => {
    render(<Keys />);
    await screen.findByText("sk-ie-cd34…");
    await userEvent.click(screen.getByRole("button", { name: /^Create key/ }));
    await screen.findByTestId("new-token");
    audit("Keys", document.body);
  });

  it("App shell (navigation and identity)", async () => {
    window.location.hash = "#/does-not-exist";
    await renderView("App shell", <App />, () => screen.findByRole("heading", { name: "Page not available" }));
  });

  it("Sign-in gate", async () => {
    vi.mocked(api.identity).mockRejectedValue(new ApiError("operator access required", 401));
    await renderView("Sign-in gate", <App />, () => screen.findByLabelText("Operator API key"));
  });

  it("explains every registry entry somewhere in the dashboard", async () => {
    await waitFor(() => expect(reports.length).toBeGreaterThanOrEqual(10));
    const shown = new Set(reports.flatMap((r) => r.explained));
    const all = [...WIRING.map((w) => w.id), ...LOCAL_CONTROLS.map((c) => c.id)];
    expect(all.filter((id) => !shown.has(id))).toEqual([]);
  });
});
