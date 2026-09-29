import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { api, ApiError, getApiKey, setApiKey, type Identity, type SystemInfo } from "../lib/api";

// The diagnostics download across the real shell: a response still pending
// when the operator forgets or changes the key, or when the session is lost,
// must not trigger a download in the next session. Only the engine API is
// mocked; the App shell and the System view are the real components.

const operator: Identity = {
  kind: "key",
  auth_required: true,
  key: { id: "k1", prefix: "sk-ie-ab12", label: "owner", role: "operator" },
};

const info = (): SystemInfo => ({
  build: { version: "0.2.0", commit: "abc1234", built_at: null },
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
    backend_kind: "llama_cpp",
    virtual_models: [],
    workload_routing_enabled: false,
    workload_rule_count: 0,
    remote_workers: [],
    spillover_providers: [],
  },
});

let resolveBundle: (v: Record<string, unknown>) => void;
let pendingBundle: Promise<Record<string, unknown>>;
let signals: (AbortSignal | undefined)[];
let clicked: HTMLAnchorElement[];

beforeEach(() => {
  window.location.hash = "#/system";
  setApiKey("sk-ie-original");
  signals = [];
  clicked = [];
  Object.assign(URL, { createObjectURL: vi.fn(() => "blob:diagnostics"), revokeObjectURL: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    clicked.push(this);
  });
  vi.spyOn(api, "identity").mockResolvedValue(operator);
  vi.spyOn(api, "system").mockResolvedValue(info());
  pendingBundle = new Promise<Record<string, unknown>>((res) => {
    resolveBundle = res;
  });
  vi.spyOn(api, "diagnostics").mockImplementation((signal?: AbortSignal) => {
    signals.push(signal);
    return pendingBundle;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  setApiKey(null);
  window.location.hash = "";
});

async function startDownload() {
  render(<App />);
  const button = await screen.findByRole("button", { name: "Download diagnostics bundle" });
  await userEvent.click(button);
  expect(button).toBeDisabled();
  expect(signals).toHaveLength(1);
}

async function deliverLateBundle() {
  await act(async () => {
    resolveBundle({ engine_version: "0.2.0", config: {} });
    // The view's own continuation was queued first; let it and any follow-up run.
    await pendingBundle;
    await new Promise((r) => setTimeout(r, 0));
  });
}

describe("diagnostics download across session changes", () => {
  it("downloads once when the session is still active", async () => {
    await startDownload();
    await deliverLateBundle();
    expect(await screen.findByText(/bundle to your downloads/)).toBeInTheDocument();
    expect(clicked).toHaveLength(1);
    expect(signals[0]?.aborted).toBe(false);
  });

  it("does not download after the operator forgets the key", async () => {
    vi.mocked(api.identity).mockResolvedValue({ kind: "local", auth_required: false, key: null });
    await startDownload();
    await userEvent.click(screen.getByRole("button", { name: "Forget key" }));
    expect(getApiKey()).toBeNull();
    await screen.findByText("Local development");
    expect(signals[0]?.aborted).toBe(true);
    await deliverLateBundle();
    expect(clicked).toHaveLength(0);
    expect(screen.queryByText(/bundle to your downloads/)).toBeNull();
    // The new session's control is idle, not stuck busy.
    expect(await screen.findByRole("button", { name: "Download diagnostics bundle" })).toBeEnabled();
  });

  it("does not download after the operator changes the key", async () => {
    await startDownload();
    await userEvent.click(screen.getByRole("button", { name: "Change key" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Operator API key"), "sk-ie-replacement");
    await userEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(getApiKey()).toBe("sk-ie-replacement"));
    await waitFor(() => expect(signals[0]?.aborted).toBe(true));
    await screen.findByRole("button", { name: "Download diagnostics bundle" });
    await deliverLateBundle();
    expect(clicked).toHaveLength(0);
    expect(screen.queryByText(/bundle to your downloads/)).toBeNull();
  });

  it("does not download after the session is lost", async () => {
    await startDownload();
    // Another request of this session finds the key revoked: the session ends.
    vi.mocked(api.system).mockRejectedValue(new ApiError("invalid or revoked key", 401));
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByRole("heading", { name: "Operator access" })).toBeInTheDocument();
    expect(signals[0]?.aborted).toBe(true);
    await deliverLateBundle();
    expect(clicked).toHaveLength(0);
  });
});
