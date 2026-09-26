import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Views are stubbed: these tests cover the shell (gate, routing, identity), and
// each view has its own suite.
// The Overview stub loads through the real useAsync hook, so a failing load
// takes the same auth-loss path as the real views.
vi.mock("../views/Overview", async () => {
  const { useAsync } = await import("../hooks/useAsync");
  const { api: client } = await import("../lib/api");
  const Overview = () => {
    const ov = useAsync(() => client.overview(), []);
    return (
      <>
        <h1>Overview view</h1>
        <p data-testid="overview-state">{ov.status === "error" ? `error: ${ov.error}` : ov.status}</p>
      </>
    );
  };
  return { Overview };
});
vi.mock("../views/Monitoring", () => ({
  Monitoring: ({ onNavigate }: { onNavigate: (v: string, o?: object) => void }) => (
    <>
      <h1>Monitoring view</h1>
      <button onClick={() => onNavigate("logs", { logQuery: "req-42" })}>open logs</button>
      <button onClick={() => onNavigate("clients", { clientId: "acme" })}>open client</button>
    </>
  ),
}));
vi.mock("../views/Clients", () => ({
  Clients: ({ focusClientId }: { focusClientId: string | null }) => (
    <h1>Clients view {focusClientId ?? "none"}</h1>
  ),
}));
// The Models stub shows the shared feature-switch state so session handling can
// be observed from the shell.
vi.mock("../views/Models", async () => {
  const { useSystem } = await import("../hooks/useSystem");
  const Models = () => {
    const sys = useSystem();
    const v = sys.switchState("allow_model_management");
    return (
      <>
        <h1>Models view</h1>
        <p data-testid="management">{v === null ? "unknown" : v ? "on" : "off"}</p>
        <p data-testid="system-status">{sys.status + (sys.stale ? " stale" : "")}</p>
        <button onClick={sys.refresh}>refresh switches</button>
      </>
    );
  };
  return { Models };
});
vi.mock("../views/Logs", () => ({
  Logs: ({ requestId }: { requestId?: string }) => <h1>Logs view {requestId ?? "all"}</h1>,
}));
// The Security stub performs protected actions the way the real views do: an
// auth failure goes to the session's reporter, anything else is shown.
vi.mock("../views/Security", async () => {
  const { useState } = await import("react");
  const { useAuthFailure } = await import("../hooks/useAuthScope");
  const { api: client } = await import("../lib/api");
  const Security = () => {
    const report = useAuthFailure();
    const [msg, setMsg] = useState("");
    const run = (n: number) =>
      Promise.all(
        Array.from({ length: n }, () =>
          client.audit({}).catch((e: Error) => {
            if (!report(e)) setMsg(e.message);
          }),
        ),
      );
    return (
      <>
        <h1>Security view</h1>
        <button onClick={() => void run(1)}>verify</button>
        <button onClick={() => void run(3)}>verify burst</button>
        <p data-testid="security-msg">{msg}</p>
      </>
    );
  };
  return { Security };
});
vi.mock("../views/Keys", () => ({ Keys: () => <h1>Keys view</h1> }));

import { App } from "../App";
import { api, ApiError, getApiKey, setApiKey, type Identity, type SystemInfo } from "../lib/api";

const operator: Identity = {
  kind: "key",
  auth_required: true,
  key: { id: "k1", prefix: "sk-ie-ab12cd", label: "owner", role: "operator" },
};

const system = (management: boolean): SystemInfo =>
  ({
    build: { version: "0.2.0", commit: null, built_at: null },
    readiness: { ready: true, checks: {}, inference: { available: false } },
    draining: false,
    switches: {
      allow_model_management: management,
      allow_network_downloads: true,
      allow_structured_output: true,
      diagnostics_enabled: true,
      require_auth: true,
      webhooks_enabled: false,
      client_events_enabled: true,
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
  }) as SystemInfo;

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function goTo(hash: string) {
  act(() => {
    window.location.hash = hash;
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

beforeEach(() => {
  window.location.hash = "";
  setApiKey(null);
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
  vi.spyOn(api, "system").mockResolvedValue(system(true));
  vi.spyOn(api, "overview").mockResolvedValue({} as never);
});

afterEach(() => vi.restoreAllMocks());

describe("App gate", () => {
  it("asks for a key on 401", async () => {
    vi.spyOn(api, "identity").mockRejectedValue(new ApiError("operator access required", 401, "operator_access_required"));
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Operator access" })).toBeInTheDocument();
    expect(screen.getByLabelText("Operator API key")).toBeInTheDocument();
  });

  it("says a rejected saved key was not accepted", async () => {
    setApiKey("sk-ie-revoked");
    vi.spyOn(api, "identity").mockRejectedValue(new ApiError("nope", 401, "operator_access_required"));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/not accepted/);
  });

  it("explains a client key instead of treating it as a bad key", async () => {
    setApiKey("sk-ie-clientkey");
    vi.spyOn(api, "identity").mockRejectedValue(
      new ApiError("this key belongs to a client", 403, "operator_role_required"),
    );
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Operator access required" })).toBeInTheDocument();
    expect(screen.getByText(/belongs to a client/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Forget saved key" }));
    expect(getApiKey()).toBeNull();
  });

  it("shows a retryable connection error for network failures and 5xx", async () => {
    const spy = vi
      .spyOn(api, "identity")
      .mockRejectedValueOnce(new ApiError("network error: failed", 0))
      .mockResolvedValueOnce(operator);
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Engine unavailable" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Operator API key")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Retry/ }));
    expect(await screen.findByRole("heading", { name: "Overview view" })).toBeInTheDocument();
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("stores a submitted key and enters the dashboard", async () => {
    vi.spyOn(api, "identity")
      .mockRejectedValueOnce(new ApiError("required", 401, "operator_access_required"))
      .mockResolvedValueOnce(operator);
    render(<App />);
    await userEvent.type(await screen.findByLabelText("Operator API key"), "sk-ie-good");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { name: "Overview view" })).toBeInTheDocument();
    expect(getApiKey()).toBe("sk-ie-good");
  });
});

describe("App routing", () => {
  beforeEach(() => {
    vi.spyOn(api, "identity").mockResolvedValue(operator);
  });

  it("opens the view named in the URL (deep link)", async () => {
    window.location.hash = "#/logs?request_id=chatcmpl-9";
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Logs view chatcmpl-9" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Logs" })).toHaveAttribute("aria-current", "page");
  });

  it("deep-links a client detail", async () => {
    window.location.hash = "#/clients/acme";
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Clients view acme" })).toBeInTheDocument();
  });

  it("follows cross-view links into shareable URLs", async () => {
    window.location.hash = "#/monitoring";
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "open logs" }));
    expect(window.location.hash).toBe("#/logs?request_id=req-42");
    expect(await screen.findByRole("heading", { name: "Logs view req-42" })).toBeInTheDocument();
  });

  it("responds to history navigation (hash changes)", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Overview view" });
    goTo("#/models");
    expect(await screen.findByRole("heading", { name: "Models view" })).toBeInTheDocument();
    goTo("#/overview");
    expect(await screen.findByRole("heading", { name: "Overview view" })).toBeInTheDocument();
  });

  it("moves focus to the page content after navigation", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Overview view" });
    expect(screen.getByRole("main")).not.toHaveFocus(); // not on first render
    goTo("#/keys");
    await screen.findByRole("heading", { name: "Keys view" });
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it("explains an unknown page instead of rendering nothing", async () => {
    window.location.hash = "#/architecture";
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Page not available" })).toBeInTheDocument();
  });

  it.each(["#/%", "#/clients/%E0%A4%A"])(
    "recovers from malformed initial URL %s",
    async (hash) => {
      window.location.hash = hash;
      render(<App />);
      expect(await screen.findByRole("heading", { name: "Page not available" })).toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "Go to Overview" }));
      expect(await screen.findByRole("heading", { name: "Overview view" })).toBeInTheDocument();
    },
  );

  it("handles malformed hash navigation and subsequent valid navigation", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "Overview view" });
    goTo("#/clients/%FF");
    expect(await screen.findByRole("heading", { name: "Page not available" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
    goTo("#/clients/acme");
    expect(await screen.findByRole("heading", { name: "Clients view acme" })).toBeInTheDocument();
  });

  it("groups navigation into labelled sections", async () => {
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    for (const label of ["Operate", "Serve", "Business", "Access"]) {
      expect(within(nav).getByRole("list", { name: label })).toBeInTheDocument();
    }
  });
});

describe("App identity menu", () => {
  it("shows the key's label, prefix, and role — never a token", async () => {
    setApiKey("sk-ie-THE-FULL-SECRET");
    vi.spyOn(api, "identity").mockResolvedValue(operator);
    render(<App />);
    const menu = await screen.findByRole("region", { name: "Signed-in identity" });
    expect(menu).toHaveTextContent("owner");
    expect(menu).toHaveTextContent("sk-ie-ab12cd…");
    expect(menu).toHaveTextContent("operator");
    expect(document.body).not.toHaveTextContent("sk-ie-THE-FULL-SECRET");
  });

  it("shows local development access when no key is needed", async () => {
    vi.spyOn(api, "identity").mockResolvedValue({ kind: "local", auth_required: false, key: null });
    render(<App />);
    const menu = await screen.findByRole("region", { name: "Signed-in identity" });
    expect(menu).toHaveTextContent("Local development");
    expect(within(menu).queryByRole("button", { name: "Forget key" })).not.toBeInTheDocument();
  });

  it("changes the key through a dialog and re-checks identity", async () => {
    setApiKey("sk-ie-old");
    const spy = vi.spyOn(api, "identity").mockResolvedValue(operator);
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Change key" }));
    const dialog = screen.getByRole("dialog", { name: "Change operator key" });
    await userEvent.type(within(dialog).getByLabelText("Operator API key"), "sk-ie-new");
    await userEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    expect(getApiKey()).toBe("sk-ie-new");
  });

  it("cancels the change-key dialog with Escape", async () => {
    setApiKey("sk-ie-old");
    vi.spyOn(api, "identity").mockResolvedValue(operator);
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Change key" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(getApiKey()).toBe("sk-ie-old");
  });

  it("forgets the key and returns to the key prompt", async () => {
    setApiKey("sk-ie-old");
    vi.spyOn(api, "identity")
      .mockResolvedValueOnce(operator)
      .mockRejectedValueOnce(new ApiError("required", 401, "operator_access_required"));
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Forget key" }));
    expect(await screen.findByRole("heading", { name: "Operator access" })).toBeInTheDocument();
    expect(getApiKey()).toBeNull();
  });
});

describe("App feature-switch session", () => {
  it("fetches switch state once per session and shows it", async () => {
    window.location.hash = "#/models";
    vi.spyOn(api, "identity").mockResolvedValue(operator);
    vi.mocked(api.system).mockResolvedValue(system(false));
    render(<App />);
    await waitFor(() => expect(screen.getByTestId("management")).toHaveTextContent("off"));
    expect(api.system).toHaveBeenCalledTimes(1);
  });

  it("opening and cancelling Change key keeps the session's switch state", async () => {
    window.location.hash = "#/models";
    setApiKey("sk-ie-old");
    vi.spyOn(api, "identity").mockResolvedValue(operator);
    vi.mocked(api.system).mockResolvedValue(system(false));
    render(<App />);
    await waitFor(() => expect(screen.getByTestId("management")).toHaveTextContent("off"));
    await userEvent.click(screen.getByRole("button", { name: "Change key" }));
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByTestId("management")).toHaveTextContent("off");
    expect(api.system).toHaveBeenCalledTimes(1);
  });

  it("a new key discards the old session's late switch response", async () => {
    window.location.hash = "#/models";
    setApiKey("sk-ie-old");
    vi.spyOn(api, "identity").mockResolvedValue(operator);
    const old = deferred<SystemInfo>();
    const fresh = deferred<SystemInfo>();
    vi.mocked(api.system).mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise);
    render(<App />);
    await screen.findByRole("heading", { name: "Models view" });
    expect(screen.getByTestId("management")).toHaveTextContent("unknown");

    await userEvent.click(screen.getByRole("button", { name: "Change key" }));
    const dialog = screen.getByRole("dialog", { name: "Change operator key" });
    await userEvent.type(within(dialog).getByLabelText("Operator API key"), "sk-ie-new");
    await userEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(api.system).toHaveBeenCalledTimes(2));

    await act(async () => fresh.resolve(system(true)));
    expect(screen.getByTestId("management")).toHaveTextContent("on");
    // The earlier session's answer arrives last and must not win.
    await act(async () => old.resolve(system(false)));
    expect(screen.getByTestId("management")).toHaveTextContent("on");
  });

  it("forgetting the key drops switch state; signing in again fetches it fresh", async () => {
    window.location.hash = "#/models";
    setApiKey("sk-ie-old");
    vi.spyOn(api, "identity")
      .mockResolvedValueOnce(operator)
      .mockRejectedValueOnce(new ApiError("required", 401, "operator_access_required"))
      .mockResolvedValueOnce(operator);
    vi.mocked(api.system).mockResolvedValueOnce(system(false)).mockResolvedValueOnce(system(true));
    render(<App />);
    await waitFor(() => expect(screen.getByTestId("management")).toHaveTextContent("off"));
    await userEvent.click(screen.getByRole("button", { name: "Forget key" }));
    await userEvent.type(await screen.findByLabelText("Operator API key"), "sk-ie-next");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(screen.getByTestId("management")).toHaveTextContent("on"));
    expect(api.system).toHaveBeenCalledTimes(2);
  });
});

describe("App Show wiring toggle", () => {
  it("is off by default, reveals endpoint chips, and persists across reloads", async () => {
    vi.spyOn(api, "identity").mockResolvedValue(operator);
    const { unmount } = render(<App />);
    const toggle = await screen.findByRole("button", { name: "Show wiring" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByTestId("wiring-chips")).not.toBeInTheDocument();

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    // The identity hint is wired: its chip names the real endpoint.
    const menu = screen.getByRole("region", { name: "Signed-in identity" });
    expect(within(menu).getByTestId("wiring-chips")).toHaveTextContent("GET /admin/identity");
    // Local-only hints (navigation) get no chip.
    const brand = screen.getByRole("button", { name: /How this works: Navigation/ }).closest(".brand") as HTMLElement;
    expect(within(brand).queryByTestId("wiring-chips")).not.toBeInTheDocument();
    expect(localStorage.getItem("ie.dashboard.showWiring")).toBe("1");

    unmount();
    render(<App />);
    expect(await screen.findByRole("button", { name: "Show wiring" })).toHaveAttribute("aria-pressed", "true");

    await userEvent.click(screen.getByRole("button", { name: "Show wiring" }));
    expect(screen.queryByTestId("wiring-chips")).not.toBeInTheDocument();
    expect(localStorage.getItem("ie.dashboard.showWiring")).toBeNull();
  });

  it("works for the session when browser storage throws", async () => {
    vi.spyOn(api, "identity").mockResolvedValue(operator);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    render(<App />);
    const toggle = await screen.findByRole("button", { name: "Show wiring" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.getAllByTestId("wiring-chips").length).toBeGreaterThan(0);
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "false");
  });
});

describe("App auth loss during a session", () => {
  const revoked = () => new ApiError("invalid or revoked API key", 401, "operator_access_required");
  const clientKey = () => new ApiError("this key belongs to a client", 403, "operator_role_required");

  async function signedInAt(hash: string) {
    window.location.hash = hash;
    setApiKey("sk-ie-old");
    const identity = vi.spyOn(api, "identity").mockResolvedValue(operator);
    render(<App />);
    await screen.findByRole("region", { name: "Signed-in identity" });
    return identity;
  }

  it("a 401 on a switch refresh ends the session: state is discarded and the key gate appears", async () => {
    vi.mocked(api.system).mockResolvedValueOnce(system(false));
    await signedInAt("#/models");
    await waitFor(() => expect(screen.getByTestId("management")).toHaveTextContent("off"));
    vi.mocked(api.system).mockRejectedValueOnce(revoked());
    await userEvent.click(screen.getByRole("button", { name: "refresh switches" }));

    expect(await screen.findByRole("heading", { name: "Operator access" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("not accepted");
    // No authenticated content, and no old switch state kept as "stale".
    expect(screen.queryByRole("heading", { name: "Models view" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("management")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Signed-in identity" })).not.toBeInTheDocument();

    // Signing in again starts a fresh session with freshly fetched state.
    vi.mocked(api.system).mockResolvedValueOnce(system(true));
    await userEvent.type(screen.getByLabelText("Operator API key"), "sk-ie-new");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(screen.getByTestId("management")).toHaveTextContent("on"));
    expect(screen.getByTestId("system-status")).toHaveTextContent(/^ready$/);
  });

  it("a 401 on an ordinary protected action ends the session", async () => {
    await signedInAt("#/security");
    vi.spyOn(api, "audit").mockRejectedValue(revoked());
    await userEvent.click(screen.getByRole("button", { name: "verify" }));
    expect(await screen.findByRole("heading", { name: "Operator access" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Security view" })).not.toBeInTheDocument();
  });

  it("a 401 on a view's data load ends the session", async () => {
    window.location.hash = "#/overview";
    setApiKey("sk-ie-old");
    vi.spyOn(api, "identity").mockResolvedValue(operator);
    vi.mocked(api.overview).mockRejectedValue(revoked());
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Operator access" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Overview view" })).not.toBeInTheDocument();
  });

  it("operator_role_required after sign-in leads to the role-denial gate", async () => {
    await signedInAt("#/security");
    vi.spyOn(api, "audit").mockRejectedValue(clientKey());
    await userEvent.click(screen.getByRole("button", { name: "verify" }));
    expect(await screen.findByRole("heading", { name: "Operator access required" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Security view" })).not.toBeInTheDocument();
  });

  it("a burst of auth failures ends the session once, with no identity re-check loop", async () => {
    const identity = await signedInAt("#/security");
    const calls = identity.mock.calls.length;
    vi.spyOn(api, "audit").mockRejectedValue(revoked());
    await userEvent.click(screen.getByRole("button", { name: "verify burst" }));
    expect(await screen.findByRole("heading", { name: "Operator access" })).toBeInTheDocument();
    expect(api.audit).toHaveBeenCalledTimes(3);
    // The failure is classified locally; no identity request storm follows.
    expect(identity.mock.calls.length).toBe(calls);
    expect(screen.getAllByRole("heading", { name: "Operator access" })).toHaveLength(1);
  });

  it("a late auth failure from the previous session does not sign out the new one", async () => {
    await signedInAt("#/security");
    let rejectOld!: (e: unknown) => void;
    vi.spyOn(api, "audit").mockReturnValueOnce(
      new Promise((_, reject) => {
        rejectOld = reject;
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "verify" }));

    // Replace the key: a new session starts.
    await userEvent.click(screen.getByRole("button", { name: "Change key" }));
    const dialog = screen.getByRole("dialog", { name: "Change operator key" });
    await userEvent.type(within(dialog).getByLabelText("Operator API key"), "sk-ie-new");
    await userEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { name: "Security view" })).toBeInTheDocument();

    await act(async () => rejectOld(revoked()));
    expect(screen.getByRole("heading", { name: "Security view" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Operator access" })).not.toBeInTheDocument();
    expect(getApiKey()).toBe("sk-ie-new");
  });

  it("a late switch response from the previous session cannot restore its state", async () => {
    const old = deferred<SystemInfo>();
    vi.mocked(api.system).mockReturnValueOnce(old.promise).mockResolvedValueOnce(system(true));
    await signedInAt("#/models");
    // End the session through a protected action while the first fetch is pending.
    goTo("#/security");
    vi.spyOn(api, "audit").mockRejectedValue(revoked());
    await userEvent.click(await screen.findByRole("button", { name: "verify" }));
    await screen.findByRole("heading", { name: "Operator access" });

    await userEvent.type(screen.getByLabelText("Operator API key"), "sk-ie-new");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    goTo("#/models");
    await waitFor(() => expect(screen.getByTestId("management")).toHaveTextContent("on"));
    await act(async () => old.resolve(system(false)));
    expect(screen.getByTestId("management")).toHaveTextContent("on");
  });

  it.each([
    ["a feature 403", () => new ApiError("model management is disabled", 403, "model_management_disabled")],
    ["an unknown 403", () => new ApiError("blocked by policy", 403, "policy_denied")],
    ["a network error", () => new ApiError("network error: down", 0)],
    ["a 5xx", () => new ApiError("engine error", 500)],
  ])("%s keeps the session and shows the error", async (_name, err) => {
    await signedInAt("#/security");
    vi.spyOn(api, "audit").mockRejectedValue(err());
    await userEvent.click(screen.getByRole("button", { name: "verify" }));
    await waitFor(() => expect(screen.getByTestId("security-msg")).toHaveTextContent(err().message));
    expect(screen.getByRole("heading", { name: "Security view" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Signed-in identity" })).toBeInTheDocument();
  });

  it("a network failure on a switch refresh keeps the session and the last known state (stale)", async () => {
    vi.mocked(api.system).mockResolvedValueOnce(system(false));
    await signedInAt("#/models");
    await waitFor(() => expect(screen.getByTestId("management")).toHaveTextContent("off"));
    vi.mocked(api.system).mockRejectedValueOnce(new ApiError("network error: down", 0));
    await userEvent.click(screen.getByRole("button", { name: "refresh switches" }));
    await waitFor(() => expect(screen.getByTestId("system-status")).toHaveTextContent("ready stale"));
    expect(screen.getByTestId("management")).toHaveTextContent("off");
    expect(screen.getByRole("heading", { name: "Models view" })).toBeInTheDocument();
  });
});
