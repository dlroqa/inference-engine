import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../hooks/useLiveMetrics", () => ({ useLiveMetrics: vi.fn() }));
vi.mock("../hooks/useLiveFeed", () => ({ useLiveFeed: vi.fn() }));

import { Overview } from "../views/Overview";
import { useLiveMetrics } from "../hooks/useLiveMetrics";
import { useLiveFeed } from "../hooks/useLiveFeed";
import { api, type MetricsSnapshot, type Overview as OverviewData, type SchedulerPanel } from "../lib/api";

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
  scheduler: {
    max_concurrency: 1,
    max_queue_depth: 32,
    queue_timeout_s: 30,
    in_use: 1,
    available: 0,
    queue_depth: 2,
    peak_in_use: 1,
    peak_queue_depth: 3,
    admitted_total: 10,
    rejected_total: 1,
    rejected_queue_full: 1,
    rejected_timeout: 0,
    cancelled_total: 0,
    slow_consumer_total: 0,
    wait_ms_avg: 40,
    wait_ms_max: 100,
    wait_ms_last: 12,
  },
};

const overviewData = (over: Partial<OverviewData> = {}): OverviewData => ({
  version: "0.1.1",
  ready: true,
  checks: { database: "ok", migrations: "applied" },
  model: { configured_id: "tiny", configured: true, state: "ready", loaded: true, model_id: "tiny" },
  metrics: snapshot,
  ...over,
});

beforeEach(() => {
  vi.spyOn(api, "overview").mockResolvedValue(overviewData());
});

afterEach(() => vi.clearAllMocks());

describe("Overview view", () => {
  it("renders counters, model banner, and meters when live", async () => {
    vi.mocked(useLiveMetrics).mockReturnValue({ snapshot, status: "live", fresh: true });
    vi.mocked(useLiveFeed).mockReturnValue({
      events: [{ type: "request.end", ts: 1_700_000_000, request_id: "chatcmpl-x", completion_tokens: 5, finish_reason: "stop", total_ms: 42 }],
      status: "live",
    });
    render(<Overview />);
    expect(screen.getByText("12")).toBeInTheDocument(); // requests total
    expect(screen.getByText(/2 queued/)).toBeInTheDocument(); // scheduler queue depth
    expect(screen.getByText(/1 shed/)).toBeInTheDocument(); // scheduler rejections
    expect(screen.getByText(/tiny · ready/)).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "CPU" })).toHaveAttribute("aria-valuenow", "33");
    expect(screen.getByText("unavailable")).toBeInTheDocument(); // energy state
    expect(screen.getByText("done")).toBeInTheDocument(); // feed row
    await screen.findByTestId("engine-readiness"); // let the readiness fetch settle
  });

  it("shows a disconnected notice when the feed drops", async () => {
    vi.mocked(useLiveMetrics).mockReturnValue({ snapshot, status: "polling", fresh: true });
    vi.mocked(useLiveFeed).mockReturnValue({ events: [], status: "down" });
    render(<Overview />);
    expect(screen.getByText(/Feed disconnected/)).toBeInTheDocument();
    // The metrics connection indicator reflects the polling fallback.
    expect(screen.getAllByRole("status").some((n) => n.textContent?.includes("Polling"))).toBe(true);
    await screen.findByTestId("engine-readiness");
  });

  it("renders safely before the first snapshot arrives", async () => {
    vi.mocked(useLiveMetrics).mockReturnValue({ snapshot: null, status: "connecting", fresh: false });
    vi.mocked(useLiveFeed).mockReturnValue({ events: [], status: "connecting" });
    render(<Overview />);
    expect(screen.getByText("No inference activity yet.")).toBeInTheDocument();
    await screen.findByTestId("engine-readiness");
  });

  it("shows the engine version and readiness from /admin/overview", async () => {
    vi.mocked(useLiveMetrics).mockReturnValue({ snapshot, status: "live", fresh: true });
    vi.mocked(useLiveFeed).mockReturnValue({ events: [], status: "live" });
    render(<Overview />);
    expect(await screen.findByTestId("engine-readiness")).toHaveTextContent("v0.1.1 · ready");
  });

  it("names the failing readiness checks", async () => {
    vi.mocked(api.overview).mockResolvedValue(
      overviewData({ ready: false, checks: { database: "ok", migrations: "pending" } }),
    );
    vi.mocked(useLiveMetrics).mockReturnValue({ snapshot, status: "live", fresh: true });
    vi.mocked(useLiveFeed).mockReturnValue({ events: [], status: "live" });
    render(<Overview />);
    expect(await screen.findByTestId("engine-readiness")).toHaveTextContent(
      "not ready (migrations: pending)",
    );
  });
});

const withScheduler = (over: Partial<SchedulerPanel> | null): MetricsSnapshot => ({
  ...snapshot,
  scheduler: over === null ? null : { ...snapshot.scheduler!, ...over },
});

function renderWith(live: { snapshot: MetricsSnapshot | null; status: "connecting" | "live" | "polling" | "down"; fresh: boolean }) {
  vi.mocked(useLiveMetrics).mockReturnValue(live);
  vi.mocked(useLiveFeed).mockReturnValue({ events: [], status: "live" });
  render(<Overview />);
  return screen.getByTestId("scheduler-card");
}

describe("Scheduler card", () => {
  it("shows actual counts and avg/max/last waits", async () => {
    const card = renderWith({ snapshot, status: "live", fresh: true });
    expect(within(card).getByRole("meter", { name: "In use" })).toHaveAttribute("aria-valuenow", "100");
    expect(within(card).getByText("1 / 1 slots")).toBeInTheDocument();
    expect(within(card).getByRole("meter", { name: "Queue" })).toHaveAttribute("aria-valuenow", "6");
    expect(within(card).getByText("2 / 32 waiting")).toBeInTheDocument();
    expect(within(card).getByText("Admitted").nextSibling).toHaveTextContent("10");
    expect(within(card).getByText("Rejected").nextSibling).toHaveTextContent("1 (queue full 1 · timeout 0)");
    expect(within(card).getByText("Cancelled").nextSibling).toHaveTextContent("0");
    expect(within(card).getByText("Wait (avg / max / last)").nextSibling).toHaveTextContent("40 ms / 100 ms / 12 ms");
    expect(within(card).getByRole("button", { name: "How this works: Scheduler" })).toBeInTheDocument();
    await screen.findByTestId("engine-readiness");
  });

  it("says no waits yet when the average is null (not a measured zero)", async () => {
    const card = renderWith({ snapshot: withScheduler({ wait_ms_avg: null }), status: "live", fresh: true });
    expect(within(card).getByText("Wait (avg / max / last)").nextSibling).toHaveTextContent("no waits yet");
    await screen.findByTestId("engine-readiness");
  });

  it("handles a zero-capacity queue without dividing by zero", async () => {
    const card = renderWith({
      snapshot: withScheduler({ max_queue_depth: 0, queue_depth: 0 }),
      status: "live",
      fresh: true,
    });
    expect(within(card).queryByRole("meter", { name: "Queue" })).toBeNull();
    expect(within(card).getByText("queueing disabled")).toBeInTheDocument();
    expect(card.textContent).not.toMatch(/NaN|Infinity/);
    await screen.findByTestId("engine-readiness");
  });

  it("says the scheduler is not running when the snapshot has none", async () => {
    const card = renderWith({ snapshot: withScheduler(null), status: "live", fresh: true });
    expect(within(card).getByText("Scheduler not running.")).toBeInTheDocument();
    // The Requests card does not invent a queue length either.
    expect(screen.queryByText(/queued/)).toBeNull();
    await screen.findByTestId("engine-readiness");
  });
});

describe("Live metrics states", () => {
  it.each(["connecting", "live", "polling"] as const)(
    "shows loading, not zeros, before the first snapshot (%s)",
    async (status) => {
      const card = renderWith({ snapshot: null, status, fresh: false });
      expect(within(card).getByRole("status", { name: "Loading scheduler state" })).toBeInTheDocument();
      for (const label of ["Requests", "Tokens", "Uptime", "Energy"]) {
        const stat = screen.getByRole("button", { name: `How this works: ${label} card` }).closest(".stat")!;
        expect(stat.querySelector(".value")).toHaveTextContent("—");
        expect(stat.querySelector(".sub")).toBeNull();
      }
      // No conclusion about a power probe without data.
      expect(screen.queryByText(/Not measured/)).toBeNull();
      expect(screen.queryByTestId("metrics-stale")).toBeNull();
      await screen.findByTestId("engine-readiness");
    },
  );

  it("shows unavailable when disconnected without any snapshot", async () => {
    const card = renderWith({ snapshot: null, status: "down", fresh: false });
    expect(within(card).getByText(/Scheduler state unavailable/)).toBeInTheDocument();
    expect(screen.getByTestId("metrics-unavailable")).toHaveTextContent(/reconnecting/);
    await screen.findByTestId("engine-readiness");
  });

  it("marks retained data as not current after a disconnect", async () => {
    renderWith({ snapshot, status: "down", fresh: false });
    expect(screen.getByTestId("metrics-stale")).toHaveTextContent(/Disconnected — showing the last data received/);
    expect(screen.getByText("12")).toBeInTheDocument(); // retained, not blanked
    await screen.findByTestId("engine-readiness");
  });

  it("marks retained data as awaiting refresh after reconnecting, before a new frame", async () => {
    renderWith({ snapshot, status: "live", fresh: false });
    expect(screen.getByTestId("metrics-stale")).toHaveTextContent(/until new data arrives/);
    await screen.findByTestId("engine-readiness");
  });

  it("shows current data with the Polling state after a successful REST result", async () => {
    renderWith({ snapshot, status: "polling", fresh: true });
    expect(screen.queryByTestId("metrics-stale")).toBeNull();
    expect(screen.getAllByRole("status").some((n) => n.textContent?.includes("Polling"))).toBe(true);
    await screen.findByTestId("engine-readiness");
  });
});

describe("Energy card", () => {
  const energyStat = () =>
    screen.getByRole("button", { name: "How this works: Energy card" }).closest(".stat")!;

  it("gives the engine's reason when not measured", async () => {
    renderWith({
      snapshot: { ...snapshot, energy: { ...snapshot.energy, reason: "no RAPL powercap interface" } },
      status: "live",
      fresh: true,
    });
    expect(energyStat().querySelector(".value")).toHaveTextContent("unavailable");
    expect(energyStat().querySelector(".sub")).toHaveTextContent("Not measured — no RAPL powercap interface");
    await screen.findByTestId("engine-readiness");
  });

  it("does not infer a measurement from the rapl source name", async () => {
    renderWith({
      snapshot: {
        ...snapshot,
        energy: { ...snapshot.energy, state: "unavailable", source: "rapl", reason: "establishing baseline" },
      },
      status: "live",
      fresh: true,
    });
    expect(energyStat().querySelector(".value")).toHaveTextContent("unavailable");
    expect(energyStat().querySelector(".sub")).toHaveTextContent("Not measured — establishing baseline");
    expect(energyStat().textContent).not.toMatch(/ W|source: rapl/);
    await screen.findByTestId("engine-readiness");
  });

  it("shows watts and the source when measured", async () => {
    renderWith({
      snapshot: {
        ...snapshot,
        energy: { state: "measured", watts: 12.34, j_per_token: 0.5, tokens_per_joule: 2, source: "rapl" },
      },
      status: "live",
      fresh: true,
    });
    expect(energyStat().querySelector(".value")).toHaveTextContent("12.3 W · 0.5 J/tok");
    expect(energyStat().querySelector(".sub")).toHaveTextContent("source: rapl");
    await screen.findByTestId("engine-readiness");
  });
});
