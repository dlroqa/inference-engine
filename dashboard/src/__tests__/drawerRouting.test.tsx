import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../hooks/useLiveMetrics", () => ({
  useLiveMetrics: () => ({ snapshot: null, status: "connecting", fresh: false }),
}));
vi.mock("../hooks/useLiveFeed", () => ({
  useLiveFeed: () => ({ events: [], status: "connecting" }),
}));

import { App } from "../App";
import { api, ApiError, setApiKey, type ModelInfo } from "../lib/api";

// The real shell with the real Models and Clients views: history entries,
// drawer provenance, and where focus lands, as the operator experiences them.

const model = (over: Partial<ModelInfo> = {}): ModelInfo => ({
  id: "m1",
  name: "tiny",
  filename: "tiny.gguf",
  source_type: "url",
  source_ref: "https://cdn.example.com/tiny.gguf",
  sha256: "abc123",
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

let rows: ModelInfo[];

function goTo(hash: string) {
  act(() => {
    window.location.hash = hash;
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

async function start(hash: string) {
  window.location.hash = hash;
  render(<App />);
  await screen.findByRole("heading", { name: "Models", level: 1 });
}

const heading = () => screen.getByRole("heading", { name: "Models", level: 1 });
const details = (name: string) => screen.getByRole("button", { name: `Details for ${name}` });

async function openFromList(name: string) {
  await userEvent.click(await screen.findByRole("button", { name: `Details for ${name}` }));
  const dialog = await screen.findByRole("dialog");
  await within(dialog).findByTestId("model-details");
  return dialog;
}

beforeEach(() => {
  setApiKey(null);
  rows = [model(), model({ id: "m 2", name: "second" })];
  vi.spyOn(api, "identity").mockResolvedValue({ kind: "local", auth_required: false, key: null });
  vi.spyOn(api, "system").mockRejectedValue(new Error("not needed"));
  vi.spyOn(api, "overview").mockResolvedValue({ version: "0.2.0", ready: true, checks: {} } as never);
  vi.spyOn(api, "listModels").mockImplementation(async () => ({ models: rows }));
  vi.spyOn(api, "getModel").mockImplementation(async (id) => {
    const found = rows.find((m) => m.id === id);
    if (!found) throw new ApiError("not found", 404, "model_not_found");
    return found;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  window.location.hash = "";
});

describe("model drawer routing and focus", () => {
  it("opening from the list adds a tagged entry; Close goes Back and focuses Details", async () => {
    await start("#/models");
    const before = window.history.length;
    const dialog = await openFromList("tiny");
    expect(window.location.hash).toBe("#/models/m1");
    expect(window.history.length).toBe(before + 1);
    expect(window.history.state).toMatchObject({ ieDrawer: "models" });
    expect(within(dialog).getByRole("button", { name: "Close" })).toHaveFocus();
    expect(screen.getByRole("main")).not.toHaveFocus();

    await userEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(window.location.hash).toBe("#/models");
    // Back to the originating entry, not a second copy of the list.
    expect(window.history.length).toBe(before + 1);
    await waitFor(() => expect(details("tiny")).toHaveFocus());
  });

  it("repeated open and close keeps returning to the list entry", async () => {
    await start("#/models");
    const before = window.history.length;
    for (let i = 0; i < 2; i++) {
      const dialog = await openFromList("tiny");
      await userEvent.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await waitFor(() => expect(details("tiny")).toHaveFocus());
      expect(dialog).not.toBeInTheDocument();
    }
    expect(window.history.length).toBe(before + 1);
    expect(window.location.hash).toBe("#/models");
  });

  it("Back closes the drawer and Forward reopens it with its provenance", async () => {
    await start("#/models");
    await openFromList("tiny");
    act(() => window.history.back());
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    act(() => window.history.forward());
    const dialog = await screen.findByRole("dialog");
    expect(window.history.state).toMatchObject({ ieDrawer: "models" });
    await within(dialog).findByTestId("model-details");
    await userEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(details("tiny")).toHaveFocus());
  });

  it("a direct link closes by replacing with the list and focuses the heading", async () => {
    await start("#/models/m1");
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByTestId("model-details");
    // The list has loaded, so a matching Details button exists; the heading
    // still gets focus because the drawer was not opened from it.
    expect(details("tiny")).toBeInTheDocument();
    const before = window.history.length;
    await userEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(window.location.hash).toBe("#/models");
    expect(window.history.length).toBe(before);
    expect(heading()).toHaveFocus();
  });

  it("decodes an encoded id once and requests it encoded once", async () => {
    await start("#/models");
    await openFromList("second");
    expect(window.location.hash).toBe("#/models/m%202");
    expect(api.getModel).toHaveBeenCalledWith("m 2");
  });

  it("a removed trigger falls back to the heading", async () => {
    await start("#/models");
    const dialog = await openFromList("second");
    rows = [model()];
    // The list's next refresh no longer has the row: the drawer says so.
    await userEvent.click(screen.getByRole("button", { name: "Refresh", hidden: true }));
    expect(await within(dialog).findByText(/no longer exists/)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(heading()).toHaveFocus());
  });

  it("leaving Models with the drawer open focuses the next page's content", async () => {
    await start("#/models");
    await openFromList("tiny");
    goTo("#/overview");
    await screen.findByRole("heading", { name: "Overview", level: 1 });
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });
});

describe("client selection focus in the shell", () => {
  beforeEach(() => {
    vi.spyOn(api, "listClients").mockResolvedValue({
      clients: [{ id: "client-1", external_ref: "cus_1", email: "a@x.io", status: "active" }],
    });
    vi.spyOn(api, "usageAttribution").mockResolvedValue({ window: "week", since: 0, keys: [] });
    vi.spyOn(api, "listWebhookEndpoints").mockResolvedValue({ endpoints: [] });
    vi.spyOn(api, "listDeliveries").mockResolvedValue({ deliveries: [] });
  });

  it("Enter selects and Space deselects with focus kept on the row button", async () => {
    window.location.hash = "#/clients";
    render(<App />);
    const button = await screen.findByRole("button", { name: "Client client-1 (a@x.io)" });
    button.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(window.location.hash).toBe("#/clients/client-1"));
    expect(await screen.findByText("Keys (usage this week)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Client client-1 (a@x.io)" })).toHaveFocus();
    expect(screen.getByRole("main")).not.toHaveFocus();

    await userEvent.keyboard(" ");
    await waitFor(() => expect(window.location.hash).toBe("#/clients"));
    await waitFor(() => expect(screen.queryByText("Keys (usage this week)")).toBeNull());
    expect(screen.getByRole("button", { name: "Client client-1 (a@x.io)" })).toHaveFocus();
  });

  it("ordinary navigation still focuses the page content", async () => {
    window.location.hash = "#/clients";
    render(<App />);
    await screen.findByRole("button", { name: "Client client-1 (a@x.io)" });
    goTo("#/clients/client-1");
    await screen.findByText("Keys (usage this week)");
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });
});
