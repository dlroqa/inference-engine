import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type JSX } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Architecture } from "../views/Architecture";
import { SystemProvider } from "../hooks/useSystem";
import { AuthScopeProvider } from "../hooks/useAuthScope";
import { api, ApiError, type BackendsPage, type SystemInfo } from "../lib/api";
import type { NodeId } from "../lib/architecture";

// The Architecture view with the engine API mocked (fault injection): loading,
// failure, stale data, recovery, late responses and focus. The live metrics
// hook runs for real; jsdom has no metrics socket, so the scheduler and
// telemetry nodes stay "not observed", which is itself asserted.

const info = (over: Partial<SystemInfo["switches"]> = {}): SystemInfo => ({
  build: { version: "0.2.0", commit: null, built_at: null },
  readiness: { ready: true, checks: { database: "ok", migrations: "applied" }, inference: { available: true, model_id: "tiny" } },
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
    ...over,
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

const pool = (available: boolean[]): BackendsPage => ({
  backends: available.map((a, i) => ({
    name: `b${i}`,
    kind: "llamacpp",
    location: "local",
    state: a ? "ready" : "failed",
    available: a,
    in_flight: 0,
    max_in_flight: 1,
    model_id: "tiny",
    served_model: null,
    context_length: 2048,
    supports_prefix_cache: false,
    supports_kv_cache_metrics: false,
    tier: "primary",
    external: false,
  })),
  ready: true,
  count: available.length,
});

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const settle = () => act(async () => new Promise((r) => setTimeout(r, 0)));

let reporter: ReturnType<typeof vi.fn>;

beforeEach(() => {
  reporter = vi.fn(() => false);
  vi.spyOn(api, "system").mockResolvedValue(info());
  vi.spyOn(api, "backends").mockResolvedValue(pool([true]));
  vi.spyOn(api, "listModels").mockResolvedValue({ models: [] });
  vi.spyOn(api, "alerts").mockResolvedValue({ alerts: [] });
  vi.spyOn(api, "metrics").mockRejectedValue(new ApiError("network error: offline", 0));
  // No metrics socket: the live hook falls back to polling, which fails.
  vi.stubGlobal(
    "WebSocket",
    class {
      constructor() {
        throw new Error("socket unavailable");
      }
    },
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// Stands in for the shell: owns the focus parameter, as the address would.
let setAddress: (v: string | null) => void = () => {};

function Harness({
  initial = null,
  onSelect,
}: {
  initial?: string | null;
  onSelect?: (id: NodeId | null) => void;
}): JSX.Element {
  const [focus, setFocus] = useState<string | null>(initial);
  setAddress = setFocus;
  return (
    <AuthScopeProvider value={reporter}>
      <SystemProvider>
        <Architecture
          focus={focus}
          onSelect={(id) => {
            onSelect?.(id);
            setFocus(id);
          }}
        />
      </SystemProvider>
    </AuthScopeProvider>
  );
}

const setFocusFromOutside = (v: string | null) => act(() => setAddress(v));

const listItem = (label: string) => screen.getByRole("heading", { name: label, level: 4 }).closest("li") as HTMLElement;

describe("Architecture view", () => {
  it("shows every node with a status in text, the diagram and the text list", async () => {
    render(<Harness />);
    await waitFor(() => expect(listItem("Backend pool")).toHaveTextContent("1 of 1 backend available"));
    expect(screen.getByRole("group", { name: "Architecture diagram" })).toBeInTheDocument();
    expect(within(listItem("Webhooks")).getByText(/none dead-lettered/)).toBeInTheDocument();
    expect(listItem("Audit log")).toHaveTextContent("Chain not checked here");
    expect(listItem("Scheduler")).toHaveTextContent(/Unknown: (Waiting for live metrics|Not observed)/);
    // Status is carried by text, including the tone word for assistive technology.
    expect(listItem("Backend pool")).toHaveTextContent("OK: 1 of 1 backend available");
    expect(within(listItem("Model registry")).getByRole("link", { name: /Open Models/ })).toHaveAttribute(
      "href",
      "#/models",
    );
    expect(listItem("Client events")).toHaveTextContent(/No operator control/);
  });

  it("shows loading, then the value, without counting what was not read", async () => {
    const d = deferred<BackendsPage>();
    vi.mocked(api.backends).mockReturnValue(d.promise);
    render(<Harness />);
    expect(await screen.findAllByText(/Loading the backend pool/)).not.toHaveLength(0);
    await act(async () => d.resolve(pool([true, false])));
    await waitFor(() => expect(listItem("Backend pool")).toHaveTextContent("Needs attention: 1 of 2 backends available"));
  });

  it("keeps stale data after a failed refresh, then recovers", async () => {
    render(<Harness />);
    await waitFor(() => expect(listItem("Backend pool")).toHaveTextContent("1 of 1 backend available"));
    vi.mocked(api.backends).mockRejectedValueOnce(new ApiError("engine error", 500));
    await userEvent.click(screen.getByRole("button", { name: /^Refresh/ }));
    await waitFor(() => expect(listItem("Backend pool")).toHaveTextContent("(stale)"));
    expect(listItem("Backend pool")).toHaveTextContent(/Last observed .*latest read failed/);
    // Independent sources are unaffected.
    expect(listItem("Webhooks")).not.toHaveTextContent("(stale)");
    await userEvent.click(screen.getByRole("button", { name: /^Refresh/ }));
    await waitFor(() => expect(listItem("Backend pool")).not.toHaveTextContent("(stale)"));
  });

  it("shows a failed first read as not observed, and a disabled feature as off", async () => {
    vi.mocked(api.alerts).mockRejectedValue(new ApiError("engine error", 500));
    vi.mocked(api.system).mockResolvedValue(info({ client_events_enabled: false }));
    render(<Harness />);
    await waitFor(() => expect(listItem("Webhooks")).toHaveTextContent("Unknown: Delivery on · dead letters not observed"));
    expect(listItem("Client events")).toHaveTextContent("Off: Client event log off (client_events_enabled=false)");
  });

  it("ignores a response that a Refresh superseded", async () => {
    const first = deferred<BackendsPage>();
    vi.mocked(api.backends).mockReturnValueOnce(first.promise).mockResolvedValueOnce(pool([true, true]));
    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: /^Refresh/ }));
    await waitFor(() => expect(listItem("Backend pool")).toHaveTextContent("2 of 2 backends available"));
    await act(async () => first.resolve(pool([false])));
    await settle();
    expect(listItem("Backend pool")).toHaveTextContent("2 of 2 backends available");
  });

  it("drops a late failure after leaving the view, without reporting auth loss", async () => {
    const d = deferred<BackendsPage>();
    vi.mocked(api.backends).mockReturnValue(d.promise);
    const { unmount } = render(<Harness />);
    await screen.findAllByText(/Loading the backend pool/);
    unmount();
    await act(async () => d.reject(new ApiError("invalid or revoked key", 401)));
    await settle();
    expect(reporter).not.toHaveBeenCalled();
  });

  it("reports auth loss from its own current read", async () => {
    vi.mocked(api.listModels).mockRejectedValue(new ApiError("invalid or revoked key", 401));
    reporter.mockReturnValue(true);
    render(<Harness />);
    await waitFor(() => expect(reporter).toHaveBeenCalledWith(expect.objectContaining({ status: 401 })));
  });
});

describe("Architecture focus", () => {
  it("focuses the linked node's details, accepting a documented alias", async () => {
    render(<Harness initial="models" />);
    const details = await screen.findByRole("heading", { name: "Model registry", level: 2 });
    expect(details).toHaveFocus();
  });

  it("explains an unknown node and focuses the page heading", async () => {
    render(<Harness initial="warp-drive" />);
    expect(await screen.findByRole("status")).toHaveTextContent("There is no warp-drive node");
    expect(screen.getByRole("heading", { name: "Architecture", level: 1 })).toHaveFocus();
    expect(screen.queryByRole("heading", { level: 2, name: "Model registry" })).toBeNull();
  });

  it("selecting a node updates the address without moving focus", async () => {
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    const button = within(screen.getByRole("group", { name: "Architecture diagram" })).getByRole("button", {
      name: /^Scheduler/,
    });
    await userEvent.click(button);
    expect(onSelect).toHaveBeenCalledWith("scheduler");
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByRole("heading", { name: "Scheduler", level: 2 })).toBeInTheDocument();
    expect(button).toHaveFocus();
  });

  it("moves focus when the address changes from outside, and not on refresh", async () => {
    render(<Harness />);
    await setFocusFromOutside("router");
    const details = await screen.findByRole("heading", { name: "Router", level: 2 });
    await waitFor(() => expect(details).toHaveFocus());
    const refresh = screen.getByRole("button", { name: /^Refresh/ });
    refresh.focus();
    await userEvent.click(refresh);
    await settle();
    expect(refresh).toHaveFocus();
  });

  it("closing the details clears the selection and keeps focus in the page", async () => {
    const onSelect = vi.fn();
    render(<Harness initial="gateway" onSelect={onSelect} />);
    await screen.findByRole("heading", { name: "Gateway", level: 2 });
    await userEvent.click(screen.getByRole("button", { name: "Close details" }));
    expect(onSelect).toHaveBeenLastCalledWith(null);
    expect(screen.queryByRole("heading", { name: "Gateway", level: 2 })).toBeNull();
    // jsdom lays nothing out, so the diagram button is not "shown": the heading takes focus.
    expect(screen.getByRole("heading", { name: "Architecture", level: 1 })).toHaveFocus();
  });
});
