import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../hooks/useLiveMetrics", () => ({ useLiveMetrics: vi.fn() }));
vi.mock("../hooks/useLiveFeed", () => ({ useLiveFeed: vi.fn() }));

import { Overview } from "../views/Overview";
import { useLiveMetrics } from "../hooks/useLiveMetrics";
import { useLiveFeed } from "../hooks/useLiveFeed";
import type { MetricsSnapshot } from "../lib/api";

const snapshot: MetricsSnapshot = {
  ts: 1_700_000_000,
  uptime_s: 3661,
  counters: {
    requests_total: 12,
    requests_active: 1,
    requests_errors: 2,
    prompt_tokens_total: 900,
    completion_tokens_total: 300,
    requests_per_min: 4,
  },
  throughput: { completion_tokens_per_s: 10 },
  resources: {
    available: true,
    cpu_percent: 33,
    memory: { total: 1000, used: 400, available: 600, percent: 40 },
    process_rss: 12345,
    disk: { path: "/data", total: 100, used: 50, free: 50, percent: 50 },
  },
  gpu: { available: false, reason: "no probe" },
  energy: { state: "unavailable", watts: null, j_per_token: null, tokens_per_joule: null, source: null },
  backend: { state: "ready", model_id: "tiny", available: true },
};

afterEach(() => vi.clearAllMocks());

describe("Overview view", () => {
  it("renders counters, model banner, and meters when live", () => {
    vi.mocked(useLiveMetrics).mockReturnValue({ snapshot, status: "live" });
    vi.mocked(useLiveFeed).mockReturnValue({
      events: [{ type: "request.end", ts: 1_700_000_000, request_id: "chatcmpl-x", completion_tokens: 5, finish_reason: "stop", total_ms: 42 }],
      status: "live",
    });
    render(<Overview />);
    expect(screen.getByText("12")).toBeInTheDocument(); // requests total
    expect(screen.getByText(/tiny · ready/)).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "CPU" })).toHaveAttribute("aria-valuenow", "33");
    expect(screen.getByText("unavailable")).toBeInTheDocument(); // energy state
    expect(screen.getByText("done")).toBeInTheDocument(); // feed row
  });

  it("shows a disconnected notice when the feed drops", () => {
    vi.mocked(useLiveMetrics).mockReturnValue({ snapshot, status: "polling" });
    vi.mocked(useLiveFeed).mockReturnValue({ events: [], status: "down" });
    render(<Overview />);
    expect(screen.getByText(/Feed disconnected/)).toBeInTheDocument();
    // The metrics connection indicator reflects the polling fallback.
    expect(screen.getAllByRole("status").some((n) => n.textContent?.includes("Polling"))).toBe(true);
  });

  it("renders safely before the first snapshot arrives", () => {
    vi.mocked(useLiveMetrics).mockReturnValue({ snapshot: null, status: "connecting" });
    vi.mocked(useLiveFeed).mockReturnValue({ events: [], status: "connecting" });
    render(<Overview />);
    expect(screen.getByText("No inference activity yet.")).toBeInTheDocument();
  });
});
