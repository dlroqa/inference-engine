import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { System } from "../views/System";
import { SystemProvider } from "../hooks/useSystem";
import { AuthScopeProvider } from "../hooks/useAuthScope";
import { api, ApiError, setApiKey, type SystemInfo } from "../lib/api";
import { SWITCH_NAMES, envVarFor, SWITCH_DETAILS } from "../lib/switches";

// The System view (A3a): read-only build, readiness and switches from the shared
// GET /admin/system state, and the diagnostics download.

const info = (over: Partial<SystemInfo> = {}, switches: Partial<SystemInfo["switches"]> = {}): SystemInfo => ({
  build: { version: "0.2.0", commit: "abc1234", built_at: "2026-09-28T00:00:00Z" },
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
    webhooks_enabled: false,
    client_events_enabled: false,
    ip_allowlist_set: false,
    grpc_enabled: false,
    ...switches,
  },
  metadata: { grpc_port: null, billing_provider: null },
  routing: {
    backend_kind: "llama_cpp",
    virtual_models: [],
    workload_routing_enabled: false,
    workload_rule_count: 0,
    remote_workers: [],
    spillover_providers: [],
  },
  ...over,
});

function renderSystem(reporter: (e: unknown) => boolean = () => false) {
  return render(
    <AuthScopeProvider value={reporter}>
      <SystemProvider>
        <System />
      </SystemProvider>
    </AuthScopeProvider>,
  );
}

let createObjectURL: ReturnType<typeof vi.fn>;
let clicked: HTMLAnchorElement[];

beforeEach(() => {
  clicked = [];
  createObjectURL = vi.fn(() => "blob:diagnostics");
  Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    clicked.push(this);
  });
});

afterEach(() => vi.restoreAllMocks());

describe("System view", () => {
  it("shows loading, then build, readiness checks and every switch read-only", async () => {
    vi.spyOn(api, "system").mockResolvedValue(info());
    renderSystem();
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
    expect(await screen.findByText("abc1234")).toBeInTheDocument();
    expect(screen.getByText("2026-09-28T00:00:00Z")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("applied")).toBeInTheDocument();
    const table = screen.getByRole("region", { name: "Feature switches table" });
    for (const name of SWITCH_NAMES) {
      // The name and the config key are usually the same text.
      expect(within(table).getAllByText(name).length).toBeGreaterThan(0);
      expect(within(table).getByText(envVarFor(SWITCH_DETAILS[name].setting))).toBeInTheDocument();
    }
    // The ip allowlist is a boolean; its setting is the list, never its contents.
    expect(within(table).getByText("IE_IP_ALLOWLIST")).toBeInTheDocument();
    // Read-only: no control in the switch table.
    expect(within(table).queryAllByRole("button")).toEqual([]);
    expect(within(table).queryAllByRole("checkbox")).toEqual([]);
  });

  it("reports not-ready, draining and a missing commit honestly", async () => {
    vi.spyOn(api, "system").mockResolvedValue(
      info({
        build: { version: "0.2.0", commit: null, built_at: null },
        draining: true,
        readiness: {
          ready: false,
          checks: { database: "ok", migrations: "applied", intake: "draining" },
          inference: { available: false, reason: "no model loaded" },
        },
      }),
    );
    renderSystem();
    expect(await screen.findByText("Not ready")).toBeInTheDocument();
    expect(screen.getByText("Draining for shutdown")).toBeInTheDocument();
    expect(screen.getAllByText("not recorded")).toHaveLength(2);
    expect(screen.getByText("no model loaded")).toBeInTheDocument();
    expect(screen.getByText(/503 while it is not/)).toBeInTheDocument();
  });

  it("shows an error with Retry when the system state cannot be loaded", async () => {
    const spy = vi.spyOn(api, "system").mockRejectedValueOnce(new ApiError("engine restarting", 503));
    spy.mockResolvedValueOnce(info());
    renderSystem();
    expect(await screen.findByRole("alert")).toHaveTextContent("engine restarting");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("abc1234")).toBeInTheDocument();
  });

  it("downloads the diagnostics bundle without rendering its contents", async () => {
    vi.spyOn(api, "system").mockResolvedValue(info());
    vi.spyOn(api, "diagnostics").mockResolvedValue({ config: { secret_marker: "DO-NOT-RENDER" } });
    renderSystem();
    await userEvent.click(await screen.findByRole("button", { name: "Download diagnostics bundle" }));
    expect(await screen.findByText(/bundle to your downloads/)).toBeInTheDocument();
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(clicked).toHaveLength(1);
    expect(clicked[0].download).toMatch(/^inference-engine-diagnostics-.*\.json$/);
    expect(document.body).not.toHaveTextContent("DO-NOT-RENDER");
  });

  it("disables the download and names the switch when diagnostics are off", async () => {
    vi.spyOn(api, "system").mockResolvedValue(info({}, { diagnostics_enabled: false }));
    const diag = vi.spyOn(api, "diagnostics");
    renderSystem();
    const button = await screen.findByRole("button", { name: "Download diagnostics bundle" });
    await waitFor(() => expect(button).toBeDisabled());
    expect(button).toHaveAccessibleDescription(/diagnostics_enabled=false/);
    expect(diag).not.toHaveBeenCalled();
  });

  it("explains a 403 diagnostics_disabled as the switch, not an authorization failure", async () => {
    const system = vi.spyOn(api, "system").mockResolvedValue(info());
    vi.spyOn(api, "diagnostics").mockRejectedValue(
      new ApiError("the diagnostics bundle is disabled by the operator", 403, "diagnostics_disabled"),
    );
    const reporter = vi.fn(() => false);
    renderSystem(reporter);
    await userEvent.click(await screen.findByRole("button", { name: "Download diagnostics bundle" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("(diagnostics_enabled=false)");
    expect(alert).not.toHaveTextContent(/Not authorized/);
    // The refusal is reported to the shared switch state, which re-reads the engine.
    await waitFor(() => expect(system).toHaveBeenCalledTimes(2));
    expect(clicked).toHaveLength(0);
  });

  it("reports any other 403 as an authorization failure", async () => {
    vi.spyOn(api, "system").mockResolvedValue(info());
    vi.spyOn(api, "diagnostics").mockRejectedValue(new ApiError("address not allowed", 403, "ip_not_allowed"));
    renderSystem();
    await userEvent.click(await screen.findByRole("button", { name: "Download diagnostics bundle" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Not authorized to download the bundle: address not allowed",
    );
  });

  it("hands operator-access loss to the session instead of showing it here", async () => {
    vi.spyOn(api, "system").mockResolvedValue(info());
    const lost = new ApiError("operator key required", 403, "operator_role_required");
    vi.spyOn(api, "diagnostics").mockRejectedValue(lost);
    const reporter = vi.fn((e: unknown) => e === lost);
    renderSystem(reporter);
    await userEvent.click(await screen.findByRole("button", { name: "Download diagnostics bundle" }));
    await waitFor(() => expect(reporter).toHaveBeenCalledWith(lost));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

// A diagnostics response that arrives after its request stopped being current
// (the view unmounted, the session was replaced, or the saved key changed) must
// not save a file, touch the view, or report anything.
describe("System view: late diagnostics responses", () => {
  function deferred<T>() {
    let resolve!: (v: T) => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  const BUNDLE = { engine_version: "0.2.0", config: {} };

  function pendingDiagnostics() {
    const d = deferred<Record<string, unknown>>();
    const signals: (AbortSignal | undefined)[] = [];
    vi.spyOn(api, "diagnostics").mockImplementation((signal?: AbortSignal) => {
      signals.push(signal);
      return d.promise;
    });
    return { d, signals };
  }

  async function settle(p: Promise<unknown>) {
    await act(async () => {
      await p.catch(() => undefined);
      await new Promise((r) => setTimeout(r, 0));
    });
  }

  afterEach(() => setApiKey(null));

  it("saves the file when the response arrives in the same active session", async () => {
    vi.spyOn(api, "system").mockResolvedValue(info());
    setApiKey("sk-ie-same");
    const { d, signals } = pendingDiagnostics();
    renderSystem();
    const button = await screen.findByRole("button", { name: "Download diagnostics bundle" });
    await userEvent.click(button);
    expect(button).toBeDisabled(); // busy while pending
    d.resolve(BUNDLE);
    await settle(d.promise);
    expect(await screen.findByText(/bundle to your downloads/)).toBeInTheDocument();
    expect(clicked).toHaveLength(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(signals[0]?.aborted).toBe(false);
    expect(button).toBeEnabled();
  });

  it("aborts the request and saves nothing when the view unmounts first", async () => {
    vi.spyOn(api, "system").mockResolvedValue(info());
    const { d, signals } = pendingDiagnostics();
    const view = renderSystem();
    await userEvent.click(await screen.findByRole("button", { name: "Download diagnostics bundle" }));
    view.unmount();
    d.resolve(BUNDLE);
    await settle(d.promise);
    expect(clicked).toHaveLength(0);
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(signals[0]?.aborted).toBe(true);
  });

  it("drops a late response when the session tree is replaced (new key or lost session)", async () => {
    vi.spyOn(api, "system").mockResolvedValue(info());
    const { d, signals } = pendingDiagnostics();
    const reporter = vi.fn(() => false);
    // The shell remounts the session tree under a new key for every session.
    const tree = (session: number) => (
      <AuthScopeProvider value={reporter}>
        <SystemProvider key={session}>
          <System />
        </SystemProvider>
      </AuthScopeProvider>
    );
    const view = render(tree(1));
    await userEvent.click(await screen.findByRole("button", { name: "Download diagnostics bundle" }));
    view.rerender(tree(2));
    d.resolve(BUNDLE);
    await settle(d.promise);
    expect(clicked).toHaveLength(0);
    expect(screen.queryByText(/bundle to your downloads/)).toBeNull();
    expect(signals[0]?.aborted).toBe(true);
    // The new session's button is idle.
    expect(await screen.findByRole("button", { name: "Download diagnostics bundle" })).toBeEnabled();
  });

  it("ignores a late failure from a replaced session instead of reporting it", async () => {
    vi.spyOn(api, "system").mockResolvedValue(info());
    const { d } = pendingDiagnostics();
    const reporter = vi.fn(() => true);
    const view = renderSystem(reporter);
    await userEvent.click(await screen.findByRole("button", { name: "Download diagnostics bundle" }));
    view.unmount();
    d.reject(new ApiError("invalid key", 401));
    await settle(d.promise);
    expect(reporter).not.toHaveBeenCalled();
  });

  it("does not save when the saved key changed while the request was pending", async () => {
    vi.spyOn(api, "system").mockResolvedValue(info());
    setApiKey("sk-ie-before");
    const { d } = pendingDiagnostics();
    renderSystem();
    await userEvent.click(await screen.findByRole("button", { name: "Download diagnostics bundle" }));
    setApiKey("sk-ie-after"); // e.g. changed in another tab
    d.resolve(BUNDLE);
    await settle(d.promise);
    expect(await screen.findByRole("alert")).toHaveTextContent("The operator key changed");
    expect(clicked).toHaveLength(0);
    expect(createObjectURL).not.toHaveBeenCalled();
  });
});
