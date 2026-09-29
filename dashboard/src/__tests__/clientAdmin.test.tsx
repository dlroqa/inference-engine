import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Clients } from "../views/Clients";
import { api, ApiError, type PlanRow, type WebhookEndpointRow } from "../lib/api";

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

  it("creates a new plan after confirming that a same-id plan would be replaced", async () => {
    await openPlans();
    await userEvent.type(screen.getByLabelText("Plan id"), "team");
    await userEvent.type(screen.getByLabelText("Name"), "Team");
    await userEvent.clear(screen.getByLabelText("Weekly quota (CU)"));
    await userEvent.type(screen.getByLabelText("Weekly quota (CU)"), "500");
    await userEvent.click(screen.getByRole("radio", { name: "Only these models" }));
    await userEvent.type(screen.getByLabelText("Model names"), "tiny, big");
    await userEvent.click(screen.getByRole("button", { name: /^Create plan/ }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Models: tiny, big");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create plan" }));
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

describe("plan contract", () => {
  const plan = (id: string, over: Partial<PlanRow> = {}): PlanRow => ({
    id,
    name: id.toUpperCase(),
    quota_5h_cu: 10,
    quota_weekly_cu: 100,
    rate_limit_per_min: null,
    allowed_models: null,
    ...over,
  });

  beforeEach(() => {
    vi.mocked(api.listPlans).mockResolvedValue({
      plans: [
        plan("pro"),
        plan("closed", { allowed_models: [] }),
        plan("some", { allowed_models: ["tiny", "big"], rate_limit_per_min: 30 }),
      ],
    });
  });

  async function openPlans() {
    render(<Clients onNavigate={vi.fn()} />);
    await screen.findByText("a@example.com");
    await userEvent.click(screen.getByRole("tab", { name: "Plans" }));
  }

  async function editAndSave(id: string, name: string) {
    await userEvent.click(await screen.findByRole("button", { name: `Edit plan ${id}` }));
    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.type(screen.getByLabelText("Name"), name);
    await userEvent.click(screen.getByRole("button", { name: /^Update plan/ }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Update plan" }));
  }

  it("keeps a deny-all plan deny-all when an unrelated field is edited", async () => {
    await openPlans();
    await editAndSave("closed", "Closed, renamed");
    await waitFor(() => expect(api.upsertPlan).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.upsertPlan).mock.calls[0][0]).toMatchObject({ name: "Closed, renamed", allowed_models: [] });
  });

  it("round-trips all models and a selected model list", async () => {
    await openPlans();
    await editAndSave("pro", "Pro 2");
    await waitFor(() => expect(api.upsertPlan).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.upsertPlan).mock.calls[0][0]).toMatchObject({ id: "pro", allowed_models: null });
    await editAndSave("some", "Some 2");
    await waitFor(() => expect(api.upsertPlan).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.upsertPlan).mock.calls[1][0]).toMatchObject({
      id: "some",
      allowed_models: ["tiny", "big"],
      rate_limit_per_min: 30,
    });
  });

  it("labels an inherited rate limit as the engine default, not as no limit", async () => {
    await openPlans();
    const row = (await screen.findByText("PRO")).closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("Engine default");
    expect(row).not.toHaveTextContent(/no limit/i);
  });

  it("shows a deny-all plan as allowing no models", async () => {
    await openPlans();
    const row = (await screen.findByText("CLOSED")).closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("No models");
    expect(row).not.toHaveTextContent(/all models/i);
  });

  it("moves between all, only-these and no models only when the operator chooses", async () => {
    await openPlans();
    await userEvent.click(await screen.findByRole("button", { name: "Edit plan some" }));
    expect(screen.getByRole("radio", { name: "Only these models" })).toBeChecked();
    expect(screen.getByLabelText("Model names")).toHaveValue("tiny, big");

    const save = async () => {
      await userEvent.click(screen.getByRole("button", { name: /^Update plan/ }));
      await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Update plan" }));
    };
    const sent = (n: number) => vi.mocked(api.upsertPlan).mock.calls[n][0].allowed_models;

    await userEvent.click(screen.getByRole("radio", { name: "No models" }));
    expect(screen.queryByLabelText("Model names")).toBeNull();
    await save();
    await waitFor(() => expect(api.upsertPlan).toHaveBeenCalledTimes(1));
    expect(sent(0)).toEqual([]);

    await userEvent.click(await screen.findByRole("button", { name: "Edit plan closed" }));
    expect(screen.getByRole("radio", { name: "No models" })).toBeChecked();
    await userEvent.click(screen.getByRole("radio", { name: "All models" }));
    await save();
    await waitFor(() => expect(api.upsertPlan).toHaveBeenCalledTimes(2));
    expect(sent(1)).toBeNull();

    await userEvent.click(await screen.findByRole("button", { name: "Edit plan pro" }));
    expect(screen.getByRole("radio", { name: "All models" })).toBeChecked();
    await userEvent.click(screen.getByRole("radio", { name: "Only these models" }));
    await userEvent.type(screen.getByLabelText("Model names"), " tiny ,, ");
    await save();
    await waitFor(() => expect(api.upsertPlan).toHaveBeenCalledTimes(3));
    expect(sent(2)).toEqual(["tiny"]);
  });

  it("refuses an empty only-these-models list instead of widening it", async () => {
    await openPlans();
    await userEvent.click(await screen.findByRole("button", { name: "Edit plan some" }));
    await userEvent.clear(screen.getByLabelText("Model names"));
    await userEvent.type(screen.getByLabelText("Model names"), " , ");
    await userEvent.click(screen.getByRole("button", { name: /^Update plan/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent('choose "No models"');
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.upsertPlan).not.toHaveBeenCalled();
  });

  it("shows 0 requests per minute as unlimited, and explains an empty rate as the engine default", async () => {
    vi.mocked(api.listPlans).mockResolvedValue({ plans: [plan("free", { rate_limit_per_min: 0 })] });
    await openPlans();
    const row = (await screen.findByText("FREE")).closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("Unlimited");
    expect(screen.getByText(/engine-wide default/)).toBeInTheDocument();
  });

  it("keeps keyboard focus sensible: Cancel first, then back to the save button", async () => {
    await openPlans();
    await userEvent.click(await screen.findByRole("button", { name: "Edit plan pro" }));
    const submit = screen.getByRole("button", { name: /^Update plan/ });
    submit.focus();
    await userEvent.keyboard("{Enter}");
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    expect(within(dialog).getByRole("button", { name: "How this works: Update plan" })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(submit).toHaveFocus();
    expect(api.upsertPlan).not.toHaveBeenCalled();
    expect(screen.getByRole("group", { name: "Allowed models" })).toContainElement(
      screen.getByRole("radio", { name: "All models" }),
    );
  });

  async function fillNewPlan(id: string) {
    await userEvent.type(screen.getByLabelText("Plan id"), id);
    await userEvent.type(screen.getByLabelText("Name"), "Team");
    await userEvent.click(screen.getByRole("button", { name: /^Create plan/ }));
  }

  it("does not save while the plan list is still loading", async () => {
    vi.mocked(api.listPlans).mockReturnValue(new Promise(() => {}));
    await openPlans();
    await fillNewPlan("pro");
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.upsertPlan).not.toHaveBeenCalled();
  });

  it("does not save when the plan list failed to load", async () => {
    vi.mocked(api.listPlans).mockRejectedValue(new ApiError("engine unavailable", 503, "unavailable"));
    await openPlans();
    await screen.findByText(/engine unavailable/);
    await fillNewPlan("pro");
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.upsertPlan).not.toHaveBeenCalled();
  });

  it("asks before saving an id the loaded list does not have, since it may exist by now", async () => {
    await openPlans();
    await screen.findByText("PRO");
    await fillNewPlan("team");
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("replaced");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api.upsertPlan).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /^Create plan/ }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Create plan" }));
    await waitFor(() => expect(api.upsertPlan).toHaveBeenCalledTimes(1));
    expect(api.upsertPlan).toHaveBeenCalledWith({
      id: "team",
      name: "Team",
      quota_5h_cu: 0,
      quota_weekly_cu: 0,
      rate_limit_per_min: null,
      allowed_models: null,
    });
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
