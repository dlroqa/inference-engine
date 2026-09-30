import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { api, ApiError, getApiKey, setApiKey, type BackendsPage, type Identity, type SystemInfo } from "../lib/api";

// The Architecture view across the real shell: a read still pending when the
// operator forgets or changes the key, or when the session is lost, must not
// change the next session's view or end it. Also: navigation entry, deep links
// and focus. Only the engine API is mocked.

const operator: Identity = {
  kind: "key",
  auth_required: true,
  key: { id: "k1", prefix: "sk-ie-ab12", label: "owner", role: "operator" },
};

const info = (): SystemInfo => ({
  build: { version: "0.2.0", commit: null, built_at: null },
  readiness: { ready: true, checks: { database: "ok", migrations: "applied" }, inference: { available: false } },
  draining: false,
  switches: {
    allow_model_management: true,
    allow_network_downloads: true,
    allow_structured_output: true,
    diagnostics_enabled: true,
    require_auth: true,
    webhooks_enabled: false,
    client_events_enabled: false,
    ip_allowlist_set: false,
    grpc_enabled: false,
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
});

const pool = (n: number): BackendsPage => ({
  backends: Array.from({ length: n }, (_, i) => ({
    name: `b${i}`,
    kind: "llamacpp",
    location: "local" as const,
    state: "ready",
    available: true,
    in_flight: 0,
    max_in_flight: 1,
    model_id: "tiny",
    served_model: null,
    context_length: 2048,
    supports_prefix_cache: false,
    supports_kv_cache_metrics: false,
    tier: "primary" as const,
    external: false,
  })),
  ready: true,
  count: n,
});

let settleOld: { resolve: (v: BackendsPage) => void; reject: (e: unknown) => void };

beforeEach(() => {
  window.location.hash = "#/architecture";
  setApiKey("sk-ie-original");
  vi.spyOn(api, "identity").mockResolvedValue(operator);
  vi.spyOn(api, "system").mockResolvedValue(info());
  vi.spyOn(api, "listModels").mockResolvedValue({ models: [] });
  vi.spyOn(api, "alerts").mockResolvedValue({ alerts: [] });
  vi.spyOn(api, "overview").mockResolvedValue({} as never);
  vi.spyOn(api, "metrics").mockRejectedValue(new ApiError("network error: offline", 0));
  vi.stubGlobal(
    "WebSocket",
    class {
      constructor() {
        throw new Error("socket unavailable");
      }
    },
  );
  // The first session's backends read stays pending; later sessions get 1 backend.
  const first = new Promise<BackendsPage>((resolve, reject) => {
    settleOld = { resolve, reject };
  });
  vi.spyOn(api, "backends").mockReturnValueOnce(first).mockResolvedValue(pool(1));
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  setApiKey(null);
  window.location.hash = "";
});

const poolItem = () => screen.getByRole("heading", { name: "Backend pool", level: 4 }).closest("li") as HTMLElement;

async function lateOld(result: "resolve" | "reject") {
  await act(async () => {
    if (result === "resolve") settleOld.resolve(pool(3));
    else settleOld.reject(new ApiError("invalid or revoked key", 401));
    await new Promise((r) => setTimeout(r, 0));
  });
}

describe("Architecture across session changes", () => {
  it("drops the old session's late read after the key is forgotten", async () => {
    vi.mocked(api.identity).mockResolvedValue({ kind: "local", auth_required: false, key: null });
    render(<App />);
    await screen.findByRole("heading", { name: "Architecture", level: 1 });
    await userEvent.click(screen.getByRole("button", { name: "Forget key" }));
    expect(getApiKey()).toBeNull();
    await screen.findByText("Local development");
    await waitFor(() => expect(poolItem()).toHaveTextContent("1 of 1 backend available"));
    await lateOld("resolve");
    expect(poolItem()).toHaveTextContent("1 of 1 backend available");
    expect(poolItem()).not.toHaveTextContent("3 of 3");
  });

  it("a late auth failure from the old key does not end the new session", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Architecture", level: 1 });
    await userEvent.click(screen.getByRole("button", { name: "Change key" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Operator API key"), "sk-ie-replacement");
    await userEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(getApiKey()).toBe("sk-ie-replacement"));
    await waitFor(() => expect(poolItem()).toHaveTextContent("1 of 1 backend available"));
    await lateOld("reject");
    expect(screen.queryByRole("heading", { name: "Operator access" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Architecture", level: 1 })).toBeInTheDocument();
  });

  it("ends the session on its own auth failure, and a late read changes nothing", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Architecture", level: 1 });
    vi.mocked(api.alerts).mockRejectedValue(new ApiError("invalid or revoked key", 401));
    await userEvent.click(screen.getByRole("button", { name: /^Refresh/ }));
    expect(await screen.findByRole("heading", { name: "Operator access" })).toBeInTheDocument();
    await lateOld("resolve");
    expect(screen.getByRole("heading", { name: "Operator access" })).toBeInTheDocument();
  });
});

describe("Architecture navigation", () => {
  it("is in the Operate section and marked current", async () => {
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    const link = within(within(nav).getByRole("list", { name: "Operate" })).getByRole("link", { name: "Architecture" });
    expect(link).toHaveAttribute("href", "#/architecture");
    expect(link).toHaveAttribute("aria-current", "page");
  });

  it("a link to a node focuses its details, not the page", async () => {
    window.location.hash = "#/system";
    render(<App />);
    await screen.findByRole("heading", { name: "System", level: 1 });
    act(() => {
      window.location.hash = "#/architecture?focus=router";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    const details = await screen.findByRole("heading", { name: "Router", level: 2 });
    await waitFor(() => expect(details).toHaveFocus());
    expect(screen.getByRole("main")).not.toHaveFocus();
  });

  it("selecting a node replaces the address instead of adding history", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Architecture", level: 1 });
    const before = window.history.length;
    const diagram = screen.getByRole("group", { name: "Architecture diagram" });
    await userEvent.click(within(diagram).getByRole("button", { name: /^Gateway/ }));
    expect(window.location.hash).toBe("#/architecture?focus=gateway");
    expect(window.history.length).toBe(before);
  });

  it("links the subsystems of a popover to their nodes, but not inside a dialog", async () => {
    window.location.hash = "#/architecture";
    render(<App />);
    await screen.findByRole("heading", { name: "Architecture", level: 1 });
    await userEvent.click(screen.getByRole("button", { name: "How this works: Architecture data" }));
    const pop = await screen.findByRole("group", { name: "How this works: Architecture data" });
    // One link per explanation, to the first subsystem of its chain.
    expect(within(pop).getByRole("link", { name: "Open Gateway in Architecture" })).toHaveAttribute(
      "href",
      "#/architecture?focus=gateway",
    );
    expect(within(pop).getAllByRole("link", { name: /in Architecture$/ })).toHaveLength(5);
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: "Change key" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "How this works: Operator sign-in" }));
    const inDialog = await within(dialog).findByRole("group", { name: /^How this works: Operator sign-in/ });
    expect(within(inDialog).queryAllByRole("link", { name: /in Architecture$/ })).toHaveLength(0);
  });
});
