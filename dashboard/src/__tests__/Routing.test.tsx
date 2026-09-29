import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Routing } from "../views/Routing";
import { SystemProvider } from "../hooks/useSystem";
import { api, ApiError, type BackendRow, type RoutePlan, type RouteRow, type SystemInfo } from "../lib/api";

// Backends & Routing (A3a): the pool and route tables keep unknown values as
// unknown, and the dry run sends only model + required features.

const backend = (over: Partial<BackendRow> = {}): BackendRow => ({
  name: "primary",
  kind: "llamacpp",
  location: "local",
  state: "ready",
  available: true,
  in_flight: 1,
  max_in_flight: 4,
  model_id: "tiny",
  served_model: null,
  context_length: 2048,
  supports_prefix_cache: false,
  supports_kv_cache_metrics: false,
  tier: "primary",
  external: false,
  ...over,
});

const route = (over: Partial<RouteRow> = {}): RouteRow => ({
  model: "tiny",
  backend: "primary",
  requests: 3,
  errors: 1,
  cancelled: 0,
  prompt_tokens: 30,
  completion_tokens: 12,
  cost: 0.00042,
  success_rate: 2 / 3,
  avg_total_ms: 120.4,
  avg_ttft_ms: null,
  avg_queue_wait_ms: 0,
  avg_output_tps: null,
  upstream_attempts: 0,
  tier: "primary",
  reasons: { model_map: 3 },
  fallbacks: {},
  policies: { base: 3 },
  workload_rules: {},
  ...over,
});

const system = (): SystemInfo => ({
  build: { version: "0.2.0", commit: null, built_at: null },
  readiness: { ready: true, checks: {}, inference: { available: true } },
  draining: false,
  switches: {
    allow_model_management: true,
    allow_network_downloads: true,
    allow_structured_output: true,
    diagnostics_enabled: true,
    require_auth: true,
    webhooks_enabled: false,
    client_events_enabled: false,
    ip_allowlist_set: false,
    grpc_enabled: false,
  },
  metadata: { grpc_port: null, billing_provider: null },
  routing: {
    backend_kind: "llama_cpp",
    virtual_models: [{ name: "auto", policy: "cascade" }],
    workload_routing_enabled: true,
    workload_rule_count: 1,
    remote_workers: ["gpu-1"],
    spillover_providers: [],
  },
});

const plan = (over: Partial<RoutePlan> = {}): RoutePlan => ({
  model: "auto",
  policy: "cascade",
  known: true,
  chosen: "gpu-1",
  chosen_step: 1,
  steps: [
    { targets: ["primary"], chosen: null, candidates: [{ name: "primary", state: "unloaded", available: false, in_flight: 0, has_capacity: true }] },
    { targets: ["gpu-1"], chosen: "gpu-1", candidates: [{ name: "gpu-1", state: "ready", available: true, in_flight: 0, has_capacity: true }] },
  ],
  workload_rule: null,
  workload_rule_preferred_targets: null,
  ...over,
});

function mock(backends: BackendRow[] = [backend()], routes: RouteRow[] = [route()]) {
  vi.spyOn(api, "system").mockResolvedValue(system());
  vi.spyOn(api, "backends").mockResolvedValue({ backends, ready: backends.some((b) => b.available), count: backends.length });
  vi.spyOn(api, "routes").mockResolvedValue({
    routes,
    sheds: {},
    totals: { requests: 3, errors: 1, cancelled: 0, cost: 0.00042, sheds: 0 },
  });
}

function renderRouting() {
  return render(
    <SystemProvider>
      <Routing />
    </SystemProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("Backends & Routing view", () => {
  it("lists the pool with state as text, capacity and honest unknowns", async () => {
    mock([
      backend(),
      backend({
        name: "gpu-1",
        kind: "vllm",
        location: "remote",
        state: "unloaded",
        available: false,
        in_flight: 0,
        model_id: null,
        served_model: "big",
        context_length: null,
        supports_prefix_cache: true,
        supports_kv_cache_metrics: true,
        tier: "spillover",
        external: true,
      }),
    ]);
    renderRouting();
    const pool = await screen.findByRole("region", { name: "Backend pool table" });
    const rows = within(pool).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("primary");
    expect(rows[1]).toHaveTextContent("1 / 4");
    expect(rows[1]).toHaveTextContent("Yes");
    expect(rows[2]).toHaveTextContent("big (configured)");
    expect(rows[2]).toHaveTextContent("external provider");
    expect(rows[2]).toHaveTextContent("spillover");
    expect(rows[2]).toHaveTextContent("No");
    expect(rows[2]).toHaveTextContent("no cache stats yet");
    expect(screen.getByText(/2 backends · pool can serve/)).toBeInTheDocument();
  });

  it("shows averages and keeps missing samples as a dash, never zero", async () => {
    mock();
    renderRouting();
    const table = await screen.findByRole("region", { name: "Per-route table" });
    const row = within(table).getAllByRole("row")[1];
    const cells = within(row).getAllByRole("cell").map((c) => c.textContent);
    expect(cells).toContain("120 ms"); // avg total 120.4, rounded
    expect(cells).toContain("0 ms"); // a measured zero queue wait stays zero
    expect(cells.filter((c) => c === "—").length).toBeGreaterThanOrEqual(2); // ttft, output rate
    expect(cells).toContain("67%");
    expect(row).toHaveTextContent("model_map ×3");
    // Never labelled as percentiles (the page says none are collected).
    expect(document.body).not.toHaveTextContent(/\bp(50|95|99)\b/);
  });

  it("shows empty states and per-panel errors with Retry", async () => {
    vi.spyOn(api, "system").mockResolvedValue(system());
    vi.spyOn(api, "backends").mockResolvedValue({ backends: [], ready: false, count: 0 });
    vi.spyOn(api, "routes").mockRejectedValueOnce(new ApiError("engine restarting", 503));
    renderRouting();
    expect(await screen.findByText("No backends are configured in the pool.")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("engine restarting");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("shows the configured routing summary from /admin/system", async () => {
    mock();
    renderRouting();
    expect(await screen.findByText("auto")).toBeInTheDocument();
    expect(screen.getByText("· cascade")).toBeInTheDocument();
    expect(screen.getByText("on · 1 rule")).toBeInTheDocument();
    expect(screen.getByText("gpu-1")).toBeInTheDocument();
  });

  it("dry-runs a route with only the model and required features, and shows each step", async () => {
    mock();
    const spy = vi.spyOn(api, "routePlan").mockResolvedValue(plan());
    renderRouting();
    await screen.findByRole("region", { name: "Backend pool table" });
    const submit = screen.getByRole("button", { name: "Plan route" });
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Model name"), "auto");
    await userEvent.click(screen.getByLabelText(/Requires structured output/));
    await userEvent.click(submit);
    expect(spy).toHaveBeenCalledWith("auto", ["structured_output"]);
    const result = await screen.findByTestId("route-plan-result");
    expect(result).toHaveTextContent("Would choose gpu-1 (step 2 of 2)");
    expect(result).toHaveTextContent("Step 1 of 2: primary");
    expect(result).toHaveTextContent("structured_output");
    expect(result).toHaveTextContent("none matched");
    expect(within(result).getAllByText("chosen")).toHaveLength(1);
  });

  it("sends exactly { model, required_features } on the wire (no prompt)", async () => {
    mock();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(plan({ steps: [], chosen: null, chosen_step: null, known: false })), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await api.routePlan("tiny", []);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/admin/route/plan");
    expect(JSON.parse(init.body as string)).toEqual({ model: "tiny", required_features: [] });
  });

  it("says when no backend could take the request and the name is unknown", async () => {
    mock();
    vi.spyOn(api, "routePlan").mockResolvedValue(
      plan({ model: "nope", policy: "base", known: false, chosen: null, chosen_step: null, steps: [{ targets: ["*all*"], chosen: null, candidates: [] }] }),
    );
    renderRouting();
    await screen.findByRole("region", { name: "Backend pool table" });
    await userEvent.type(screen.getByLabelText("Model name"), "nope{Enter}");
    const result = await screen.findByTestId("route-plan-result");
    expect(result).toHaveTextContent("No backend could take this request right now");
    expect(result).toHaveTextContent("Not a known model or virtual model name.");
    expect(result).toHaveTextContent("Candidates in whole pool");
    expect(result).toHaveTextContent("No eligible backend in this scope.");
  });

  it("reports a rejected dry run", async () => {
    mock();
    vi.spyOn(api, "routePlan").mockRejectedValue(new ApiError("required_features.0: bad", 400));
    renderRouting();
    await screen.findByRole("region", { name: "Backend pool table" });
    await userEvent.type(screen.getByLabelText("Model name"), "tiny{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("The dry run failed: required_features.0: bad");
  });

  it("links the routing configuration docs and offers no configuration controls", async () => {
    mock();
    renderRouting();
    await screen.findByRole("region", { name: "Backend pool table" });
    const link = screen.getByRole("link", { name: /Backends and routing documentation/ });
    expect(link).toHaveAttribute("href", "https://github.com/dlroqa/inference-engine/blob/main/docs/backends.md");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    // Only Refresh, the dry-run form and help buttons: nothing edits configuration.
    const buttons = screen
      .getAllByRole("button")
      .filter((b) => !b.classList.contains("wired-btn"))
      .map((b) => b.textContent?.trim());
    expect(buttons).toEqual(["Refresh", "Plan route"]);
  });
});
