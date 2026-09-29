import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Clients } from "../views/Clients";
import { api, ApiError, type WebhookEndpointRow } from "../lib/api";

const endpoint = (over: Partial<WebhookEndpointRow> = {}): WebhookEndpointRow => ({
  id: "e1",
  client_id: "c1",
  url: "https://hooks.example.com/a",
  description: null,
  disabled: false,
  event_types: null,
  ...over,
});

const delivery = (id: string, status: string) => ({
  id,
  endpoint_id: "e1",
  event_id: `ev-${id}`,
  event_type: "subscription.activated",
  status,
  attempts: 1,
  next_attempt_at: 0,
  last_status_code: status === "dead" ? 500 : 200,
  last_error: null,
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
});

beforeEach(() => {
  vi.spyOn(api, "listClients").mockResolvedValue({
    clients: [{ id: "c1", external_ref: "cus_1", email: "a@example.com", status: "active" }],
  });
  vi.spyOn(api, "usageAttribution").mockResolvedValue({ window: "week", since: 0, keys: [] });
  vi.spyOn(api, "listWebhookEndpoints").mockResolvedValue({ endpoints: [endpoint()] });
  vi.spyOn(api, "listDeliveries").mockResolvedValue({ deliveries: [delivery("d1", "dead")] });
  vi.spyOn(api, "listPlans").mockResolvedValue({
    plans: [{ id: "pro", name: "Pro", quota_5h_cu: 10, quota_weekly_cu: 100, rate_limit_per_min: null, allowed_models: null }],
  });
  vi.spyOn(api, "upsertPlan").mockImplementation(async (p) => p);
  vi.spyOn(api, "setEndpointDisabled").mockImplementation(async (id, disabled) => ({ id, disabled }));
  vi.spyOn(api, "deleteEndpoint").mockImplementation(async (id) => ({ id, deleted: true }));
  vi.spyOn(api, "replayDelivery").mockImplementation(async (id) => ({ id, status: "pending" }));
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function openClient() {
  render(<Clients focusClientId="c1" onNavigate={vi.fn()} />);
  await screen.findByText("https://hooks.example.com/a");
}

describe("plans", () => {
  async function openPlans() {
    render(<Clients onNavigate={vi.fn()} />);
    await screen.findByText("a@example.com");
    await userEvent.click(screen.getByRole("tab", { name: "Plans" }));
    await screen.findByText("Pro");
  }

  it("creates a new plan without a confirmation", async () => {
    await openPlans();
    await userEvent.type(screen.getByLabelText("Plan id"), "team");
    await userEvent.type(screen.getByLabelText("Name"), "Team");
    await userEvent.clear(screen.getByLabelText("Weekly quota (CU)"));
    await userEvent.type(screen.getByLabelText("Weekly quota (CU)"), "500");
    await userEvent.type(screen.getByLabelText("Allowed models (optional)"), "tiny, big");
    await userEvent.click(screen.getByRole("button", { name: /^Create plan/ }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() =>
      expect(api.upsertPlan).toHaveBeenCalledWith({
        id: "team",
        name: "Team",
        quota_5h_cu: 0,
        quota_weekly_cu: 500,
        rate_limit_per_min: null,
        allowed_models: ["tiny", "big"],
      }),
    );
    expect(await screen.findByText("Created plan team.")).toBeInTheDocument();
  });

  it("asks before replacing an existing plan, and Cancel sends nothing", async () => {
    await openPlans();
    await userEvent.click(screen.getByRole("button", { name: "Edit plan pro" }));
    expect(screen.getByLabelText("Plan id")).toHaveValue("pro");
    await userEvent.click(screen.getByRole("button", { name: /^Update plan/ }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Every client on pro gets the new limits");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api.upsertPlan).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /^Update plan/ }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Update plan" }));
    await waitFor(() => expect(api.upsertPlan).toHaveBeenCalledTimes(1));
  });
});

describe("webhook endpoint actions", () => {
  it("asks before disabling, and says queued deliveries are dead-lettered", async () => {
    await openClient();
    await userEvent.click(screen.getByRole("button", { name: "Disable endpoint https://hooks.example.com/a" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("dead-lettered without being sent");
    await userEvent.click(within(dialog).getByRole("button", { name: "Disable endpoint" }));
    await waitFor(() => expect(api.setEndpointDisabled).toHaveBeenCalledWith("e1", true));
  });

  it("enables a disabled endpoint without a confirmation", async () => {
    vi.mocked(api.listWebhookEndpoints).mockResolvedValue({ endpoints: [endpoint({ disabled: true })] });
    await openClient();
    await userEvent.click(screen.getByRole("button", { name: "Enable endpoint https://hooks.example.com/a" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(api.setEndpointDisabled).toHaveBeenCalledWith("e1", false));
  });

  it("deletes only after confirmation", async () => {
    await openClient();
    const del = screen.getByRole("button", { name: "Delete endpoint https://hooks.example.com/a" });
    await userEvent.click(del);
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(api.deleteEndpoint).not.toHaveBeenCalled();
    await userEvent.click(del);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("signing secrets and its whole delivery history");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete endpoint" }));
    await waitFor(() => expect(api.deleteEndpoint).toHaveBeenCalledWith("e1"));
  });

  it("shows the engine's reason when an endpoint is refused", async () => {
    vi.spyOn(api, "createWebhookEndpoint").mockRejectedValue(
      new ApiError("endpoint host must be ASCII; use the xn-- (punycode) form", 400, "endpoint_host_not_ascii"),
    );
    await openClient();
    await userEvent.type(screen.getByLabelText("URL"), "https://bücher.example.com/h");
    await userEvent.click(screen.getByRole("button", { name: /^Add endpoint/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("use the xn-- (punycode) form");
    expect(screen.queryByTestId("new-endpoint-secret")).toBeNull();
  });
});

describe("deliveries", () => {
  it("filters by status on the server", async () => {
    await openClient();
    await screen.findByText("subscription.activated");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "dead");
    await waitFor(() =>
      expect(api.listDeliveries).toHaveBeenLastCalledWith({ endpoint_id: "e1", limit: 100, status: "dead" }),
    );
  });

  it("replays after confirmation, and a pending delivery cannot be replayed", async () => {
    vi.mocked(api.listDeliveries).mockResolvedValue({
      deliveries: [delivery("d1", "dead"), { ...delivery("d2", "pending"), created_at: 1_600_000_000 }],
    });
    await openClient();
    const buttons = await screen.findAllByRole("button", { name: /^Replay subscription\.activated delivery/ });
    expect(buttons[1]).toBeDisabled(); // the pending one
    await userEvent.click(buttons[0]);
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("de-duplicates by the webhook-id header");
    await userEvent.click(within(dialog).getByRole("button", { name: "Replay delivery" }));
    await waitFor(() => expect(api.replayDelivery).toHaveBeenCalledWith("d1"));
  });
});
