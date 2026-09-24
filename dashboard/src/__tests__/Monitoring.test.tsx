import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Monitoring } from "../views/Monitoring";
import { api } from "../lib/api";

vi.mock("../hooks/useLiveMetrics", () => ({
  useLiveMetrics: () => ({ snapshot: null, status: "live" }),
}));
vi.mock("../hooks/useLiveFeed", () => ({
  useLiveFeed: () => ({
    events: [{ type: "request.end", ts: 1_700_000_000, request_id: "req-abc123", model: "m" }],
    status: "live",
  }),
}));

afterEach(() => vi.restoreAllMocks());

function mockAll() {
  vi.spyOn(api, "alerts").mockResolvedValue({ alerts: [] });
  vi.spyOn(api, "usageAttribution").mockResolvedValue({ window: "5h", since: 0, keys: [] });
  vi.spyOn(api, "errorTaxonomy").mockResolvedValue({ window: "5h", since: 0, categories: {}, total: 0 });
}

describe("Monitoring view", () => {
  it("shows alerts with a client cross-link", async () => {
    vi.spyOn(api, "usageAttribution").mockResolvedValue({ window: "5h", since: 0, keys: [] });
    vi.spyOn(api, "errorTaxonomy").mockResolvedValue({ window: "5h", since: 0, categories: {}, total: 0 });
    vi.spyOn(api, "alerts").mockResolvedValue({
      alerts: [
        { severity: "warning", kind: "client_suspended", message: "Client c1 is suspended", target_type: "client", target_id: "c1" },
      ],
    });
    const onNavigate = vi.fn();
    render(<Monitoring onNavigate={onNavigate} />);
    expect(await screen.findByText("Client c1 is suspended")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /View client/ }));
    expect(onNavigate).toHaveBeenCalledWith("clients", { clientId: "c1" });
  });

  it("renders the error taxonomy breakdown", async () => {
    mockAll();
    vi.spyOn(api, "errorTaxonomy").mockResolvedValue({
      window: "5h", since: 0, categories: { auth: 3, backend: 1 }, total: 4,
    });
    render(<Monitoring onNavigate={vi.fn()} />);
    expect(await screen.findByText("auth")).toBeInTheDocument();
    expect(screen.getByText("backend")).toBeInTheDocument();
    expect(screen.getByText("4 categorized errors")).toBeInTheDocument();
  });

  it("cross-links a feed row to logs by request id", async () => {
    mockAll();
    const onNavigate = vi.fn();
    render(<Monitoring onNavigate={onNavigate} />);
    const btn = await screen.findByRole("button", { name: /View logs for request req-abc123/ });
    await userEvent.click(btn);
    expect(onNavigate).toHaveBeenCalledWith("logs", { logQuery: "req-abc123" });
  });

  it("shows attribution rows", async () => {
    vi.spyOn(api, "alerts").mockResolvedValue({ alerts: [] });
    vi.spyOn(api, "errorTaxonomy").mockResolvedValue({ window: "5h", since: 0, categories: {}, total: 0 });
    vi.spyOn(api, "usageAttribution").mockResolvedValue({
      window: "5h", since: 0,
      keys: [{
        key_id: "k1", key_prefix: "sk-ie-ab12", key_label: "app", client_id: "c1",
        requests: 10, prompt_tokens: 100, completion_tokens: 50, cu: 150, errors: 2, last_ts: 1_700_000_000,
      }],
    });
    render(<Monitoring onNavigate={vi.fn()} />);
    expect(await screen.findByText("sk-ie-ab12")).toBeInTheDocument();
    expect(screen.getByText("100 / 50")).toBeInTheDocument();
  });
});
