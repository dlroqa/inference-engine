import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import {
  api,
  ApiError,
  getApiKey,
  setApiKey,
  type CreatedClientKey,
  type Identity,
  type RotatedSecret,
  type SystemInfo,
} from "../lib/api";

// One-time secrets across the real shell. A client key's token or a webhook
// signing secret whose response arrives after the operator forgets or changes
// the key, after the session is lost, after switching to another client, or
// after leaving the view, must never be shown, and its request is aborted.
// Only the engine API is mocked; the App shell and the Clients view are real.

const TOKEN = "sk-ie-late-token-0123456789";
const SECRET = "whsec_late_rotated_secret";

const operator: Identity = {
  kind: "key",
  auth_required: true,
  key: { id: "k1", prefix: "sk-ie-ab12", label: "owner", role: "operator" },
};

const info = (): SystemInfo => ({
  build: { version: "0.2.0", commit: "abc1234", built_at: null },
  readiness: { ready: true, checks: { database: "ok" }, inference: { available: false } },
  draining: false,
  switches: {
    allow_model_management: true,
    allow_network_downloads: true,
    allow_structured_output: true,
    diagnostics_enabled: true,
    require_auth: true,
    webhooks_enabled: true,
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

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

let keyReply: ReturnType<typeof deferred<CreatedClientKey>>;
let rotateReply: ReturnType<typeof deferred<RotatedSecret>>;
let signals: (AbortSignal | undefined)[];

beforeEach(() => {
  window.location.hash = "#/clients/c1";
  setApiKey("sk-ie-original");
  signals = [];
  keyReply = deferred<CreatedClientKey>();
  rotateReply = deferred<RotatedSecret>();
  vi.spyOn(api, "identity").mockResolvedValue(operator);
  vi.spyOn(api, "system").mockResolvedValue(info());
  vi.spyOn(api, "listClients").mockResolvedValue({
    clients: [
      { id: "c1", external_ref: null, email: "one@example.com", status: "active" },
      { id: "c2", external_ref: null, email: "two@example.com", status: "active" },
    ],
  });
  vi.spyOn(api, "usageAttribution").mockResolvedValue({ window: "week", since: 0, keys: [] });
  vi.spyOn(api, "listWebhookEndpoints").mockImplementation(async (clientId?: string) => ({
    endpoints: [
      {
        id: `ep-${clientId}`,
        client_id: clientId ?? "",
        url: `https://hooks.example.com/${clientId}`,
        description: null,
        disabled: false,
        event_types: null,
      },
    ],
  }));
  vi.spyOn(api, "listDeliveries").mockResolvedValue({ deliveries: [] });
  vi.spyOn(api, "createClientKey").mockImplementation((_id, _label, signal) => {
    signals.push(signal);
    return keyReply.promise;
  });
  vi.spyOn(api, "rotateEndpointSecret").mockImplementation((_id, signal) => {
    signals.push(signal);
    return rotateReply.promise;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  setApiKey(null);
  window.location.hash = "";
});

const created = (): CreatedClientKey => ({ id: "k9", prefix: "sk-ie-late", label: null, client_id: "c1", token: TOKEN });
const rotated = (): RotatedSecret => ({
  id: "ep-c1",
  secret: SECRET,
  grace_s: 3600,
  previous_secret_expires_at: 1_900_000_000,
});

async function startKeyCreation() {
  render(<App />);
  const button = await screen.findByRole("button", { name: /^Create client key/ });
  await userEvent.click(button);
  expect(button).toBeDisabled();
  expect(signals).toHaveLength(1);
}

async function deliver<T>(reply: { promise: Promise<T>; resolve: (v: T) => void }, value: T) {
  await act(async () => {
    reply.resolve(value);
    await reply.promise;
    await new Promise((r) => setTimeout(r, 0));
  });
}

function pageHas(text: string): boolean {
  return (document.body.textContent ?? "").includes(text);
}

describe("one-time client key token across session changes", () => {
  it("shows the token once in the active session, until dismissed", async () => {
    await startKeyCreation();
    await deliver(keyReply, created());
    expect(await screen.findByTestId("new-client-token")).toHaveTextContent(TOKEN);
    expect(signals[0]?.aborted).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "I have saved it" }));
    expect(pageHas(TOKEN)).toBe(false);
  });

  it("is not shown after the operator forgets the key", async () => {
    vi.mocked(api.identity).mockResolvedValue({ kind: "local", auth_required: false, key: null });
    await startKeyCreation();
    await userEvent.click(screen.getByRole("button", { name: "Forget key" }));
    expect(getApiKey()).toBeNull();
    await screen.findByText("Local development");
    await deliver(keyReply, created());
    expect(pageHas(TOKEN)).toBe(false);
    expect(signals[0]?.aborted).toBe(true);
    // The new session's control is idle, not stuck busy.
    expect(await screen.findByRole("button", { name: /^Create client key/ })).toBeEnabled();
  });

  it("is not shown after the operator changes the key", async () => {
    await startKeyCreation();
    await userEvent.click(screen.getByRole("button", { name: "Change key" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Operator API key"), "sk-ie-replacement");
    await userEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(getApiKey()).toBe("sk-ie-replacement"));
    await screen.findByRole("button", { name: /^Create client key/ });
    await deliver(keyReply, created());
    expect(pageHas(TOKEN)).toBe(false);
    expect(signals[0]?.aborted).toBe(true);
  });

  it("is not shown after the session is lost", async () => {
    await startKeyCreation();
    // Another request of this session finds the key revoked: the session ends.
    vi.mocked(api.listClients).mockRejectedValue(new ApiError("invalid or revoked key", 401));
    await userEvent.click(screen.getAllByRole("button", { name: "Refresh" })[0]);
    expect(await screen.findByRole("heading", { name: "Operator access" })).toBeInTheDocument();
    await deliver(keyReply, created());
    expect(pageHas(TOKEN)).toBe(false);
    expect(signals[0]?.aborted).toBe(true);
  });

  it("is not shown in another client's panel after switching clients", async () => {
    await startKeyCreation();
    await userEvent.click(screen.getByRole("button", { name: /^Client c2/ }));
    await screen.findByText("https://hooks.example.com/c2");
    await deliver(keyReply, created());
    expect(pageHas(TOKEN)).toBe(false);
    expect(signals[0]?.aborted).toBe(true);
  });

  it("is not shown after leaving the view and coming back", async () => {
    await startKeyCreation();
    await act(async () => {
      window.location.hash = "#/keys";
    });
    await screen.findByRole("heading", { name: "API keys" });
    await deliver(keyReply, created());
    await act(async () => {
      window.location.hash = "#/clients/c1";
    });
    await screen.findByRole("button", { name: /^Create client key/ });
    expect(pageHas(TOKEN)).toBe(false);
    expect(signals[0]?.aborted).toBe(true);
  });
});

describe("rotated signing secret across session changes", () => {
  async function startRotation() {
    render(<App />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Rotate secret for endpoint https://hooks.example.com/c1" }),
    );
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Rotate secret" }));
    expect(signals).toHaveLength(1);
  }

  it("shows the secret and its grace period in the active session", async () => {
    await startRotation();
    await deliver(rotateReply, rotated());
    expect(await screen.findByTestId("rotated-secret")).toHaveTextContent(SECRET);
    expect(screen.getByTestId("rotation-grace")).toHaveTextContent("1 hour grace period");
  });

  it("is not shown after switching clients", async () => {
    await startRotation();
    await userEvent.click(screen.getByRole("button", { name: /^Client c2/ }));
    await screen.findByText("https://hooks.example.com/c2");
    await deliver(rotateReply, rotated());
    expect(pageHas(SECRET)).toBe(false);
    expect(signals[0]?.aborted).toBe(true);
  });

  it("is not shown after the operator forgets the key", async () => {
    vi.mocked(api.identity).mockResolvedValue({ kind: "local", auth_required: false, key: null });
    await startRotation();
    await userEvent.click(screen.getByRole("button", { name: "Forget key" }));
    await screen.findByText("Local development");
    await deliver(rotateReply, rotated());
    expect(pageHas(SECRET)).toBe(false);
    expect(signals[0]?.aborted).toBe(true);
  });
});
