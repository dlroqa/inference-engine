import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Clients } from "../views/Clients";
import { api } from "../lib/api";

afterEach(() => vi.restoreAllMocks());

function baseMocks() {
  vi.spyOn(api, "listClients").mockResolvedValue({
    clients: [
      { id: "client-1", external_ref: "cus_1", email: "a@x.io", status: "active" },
      { id: "client-2", external_ref: "cus_2", email: "b@x.io", status: "suspended" },
    ],
  });
  vi.spyOn(api, "usageAttribution").mockResolvedValue({
    window: "week", since: 0,
    keys: [{
      key_id: "k1", key_prefix: "sk-ie-ab12", key_label: "app", client_id: "client-1",
      requests: 5, prompt_tokens: 10, completion_tokens: 5, cu: 42, errors: 0, last_ts: null,
    }],
  });
  vi.spyOn(api, "listWebhookEndpoints").mockResolvedValue({ endpoints: [] });
  vi.spyOn(api, "listDeliveries").mockResolvedValue({ deliveries: [] });
}

describe("Clients view", () => {
  it("lists clients with status and aggregated usage", async () => {
    baseMocks();
    render(<Clients onNavigate={vi.fn()} />);
    expect(await screen.findByText("a@x.io")).toBeInTheDocument();
    expect(screen.getByText("suspended")).toBeInTheDocument();
    // client-1 aggregated CU (42) appears.
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("opens a client detail drawer on row click", async () => {
    baseMocks();
    render(<Clients onNavigate={vi.fn()} />);
    await screen.findByText("a@x.io");
    await userEvent.click(screen.getByText("a@x.io"));
    expect(await screen.findByText("Keys (usage this week)")).toBeInTheDocument();
    expect(screen.getByText("Webhook endpoints")).toBeInTheDocument();
    await waitFor(() => expect(api.listWebhookEndpoints).toHaveBeenCalledWith("client-1"));
  });

  it("auto-selects a focused client from cross-link", async () => {
    baseMocks();
    render(<Clients focusClientId="client-2" onNavigate={vi.fn()} />);
    expect(await screen.findByText("Keys (usage this week)")).toBeInTheDocument();
    await waitFor(() => expect(api.listWebhookEndpoints).toHaveBeenCalledWith("client-2"));
  });

  it("shows an error state when the client list fails", async () => {
    vi.spyOn(api, "listClients").mockRejectedValue(new Error("down"));
    vi.spyOn(api, "usageAttribution").mockResolvedValue({ window: "week", since: 0, keys: [] });
    render(<Clients onNavigate={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("down");
  });

  it("requests deliveries per endpoint on the server, not a global page", async () => {
    baseMocks();
    vi.mocked(api.listWebhookEndpoints).mockResolvedValue({
      endpoints: [
        { id: "e1", client_id: "client-1", url: "https://h/1", description: null, disabled: false, event_types: null },
        { id: "e2", client_id: "client-1", url: "https://h/2", description: null, disabled: true, event_types: null },
      ],
    });
    const delivery = (id: string, endpoint_id: string, event_type: string, created_at: number) => ({
      id, endpoint_id, event_id: `ev-${id}`, event_type, status: "delivered", attempts: 1,
      next_attempt_at: 0, last_status_code: 200, last_error: null, created_at, updated_at: created_at,
    });
    vi.mocked(api.listDeliveries).mockImplementation(async (params = {}) => ({
      deliveries:
        params.endpoint_id === "e1"
          ? [delivery("d1", "e1", "usage.threshold.reached", 100)]
          : [delivery("d2", "e2", "subscription.canceled", 200)],
    }));
    render(<Clients focusClientId="client-1" onNavigate={vi.fn()} />);
    expect(await screen.findByText("usage.threshold.reached")).toBeInTheDocument();
    expect(screen.getByText("subscription.canceled")).toBeInTheDocument();
    expect(api.listDeliveries).toHaveBeenCalledWith({ endpoint_id: "e1", limit: 100 });
    expect(api.listDeliveries).toHaveBeenCalledWith({ endpoint_id: "e2", limit: 100 });
    for (const [params] of vi.mocked(api.listDeliveries).mock.calls) {
      expect(params?.endpoint_id).toBeDefined();
    }
  });

  it("selects and deselects a client from the keyboard with its row button", async () => {
    baseMocks();
    const nav = vi.fn();
    render(<Clients onNavigate={nav} />);
    await screen.findByText("a@x.io");
    const button = screen.getByRole("button", { name: "Client client-1 (a@x.io)" });
    expect(button).toHaveAttribute("aria-pressed", "false");

    button.focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByText("Keys (usage this week)")).toBeInTheDocument();
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveFocus();
    expect(nav).toHaveBeenCalledTimes(1);
    expect(nav).toHaveBeenLastCalledWith("clients", { clientId: "client-1", keepFocus: true });

    await userEvent.keyboard(" ");
    await waitFor(() => expect(screen.queryByText("Keys (usage this week)")).toBeNull());
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).toHaveFocus();
    expect(nav).toHaveBeenCalledTimes(2);
    expect(nav).toHaveBeenLastCalledWith("clients", { keepFocus: true });
  });

  it("navigates once per activation, from the button or the row", async () => {
    baseMocks();
    const nav = vi.fn();
    render(<Clients onNavigate={nav} />);
    await screen.findByText("a@x.io");
    // A click on the button does not also run the row's handler.
    await userEvent.click(screen.getByRole("button", { name: "Client client-2 (b@x.io)" }));
    expect(nav).toHaveBeenCalledTimes(1);
    expect(nav).toHaveBeenLastCalledWith("clients", { clientId: "client-2", keepFocus: true });
    // Pointer selection anywhere on the row still works (once).
    await userEvent.click(screen.getByText("a@x.io"));
    expect(nav).toHaveBeenCalledTimes(2);
    expect(nav).toHaveBeenLastCalledWith("clients", { clientId: "client-1", keepFocus: false });
  });
});
