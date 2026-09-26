import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Views are stubbed: these tests cover the shell (gate, routing, identity), and
// each view has its own suite.
vi.mock("../views/Overview", () => ({ Overview: () => <h1>Overview view</h1> }));
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
vi.mock("../views/Models", () => ({ Models: () => <h1>Models view</h1> }));
vi.mock("../views/Logs", () => ({
  Logs: ({ requestId }: { requestId?: string }) => <h1>Logs view {requestId ?? "all"}</h1>,
}));
vi.mock("../views/Security", () => ({ Security: () => <h1>Security view</h1> }));
vi.mock("../views/Keys", () => ({ Keys: () => <h1>Keys view</h1> }));

import { App } from "../App";
import { api, ApiError, getApiKey, setApiKey, type Identity } from "../lib/api";

const operator: Identity = {
  kind: "key",
  auth_required: true,
  key: { id: "k1", prefix: "sk-ie-ab12cd", label: "owner", role: "operator" },
};

function goTo(hash: string) {
  act(() => {
    window.location.hash = hash;
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

beforeEach(() => {
  window.location.hash = "";
  setApiKey(null);
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

  it("explains an unknown page instead of rendering nothing", async () => {
    window.location.hash = "#/architecture";
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Page not available" })).toBeInTheDocument();
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
