import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { JSX } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Models } from "../views/Models";
import { SystemProvider, useSystem } from "../hooks/useSystem";
import { api, ApiError, type ModelInfo, type SwitchName, type SystemInfo } from "../lib/api";
import {
  FEATURE_ERROR_SWITCH,
  SWITCH_LABELS,
  SWITCH_NAMES,
  describeActionError,
  featureSwitchFor,
  switchReason,
} from "../lib/switches";
import { WIRING } from "../lib/wiring";

// Feature-switch explanations driven by GET /admin/system (A2b). The UI only
// explains; the engine enforces every switch.

const system = (switches: Partial<SystemInfo["switches"]> = {}): SystemInfo => ({
  build: { version: "0.2.0", commit: null, built_at: null },
  readiness: { ready: true, checks: { database: "ok", migrations: "applied" }, inference: { available: false } },
  draining: false,
  switches: {
    allow_model_management: true,
    allow_network_downloads: true,
    allow_structured_output: true,
    diagnostics_enabled: true,
    require_auth: true,
    webhooks_enabled: false,
    client_events_enabled: true,
    ip_allowlist_set: false,
    grpc_enabled: false,
    ...switches,
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
});

const model = (over: Partial<ModelInfo> = {}): ModelInfo => ({
  id: "m1",
  name: "tiny",
  filename: "tiny.gguf",
  source_type: "url",
  source_ref: "http://x/tiny.gguf",
  sha256: "abc",
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

const ROWS = [
  model({ id: "m1", name: "ready" }),
  model({ id: "m2", name: "loaded", loaded: true, active: true }),
  model({ id: "m3", name: "fetching", status: "downloading", progress: 0.5, downloaded_bytes: 500 }),
];

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderModels(): void {
  render(
    <SystemProvider>
      <Models />
    </SystemProvider>,
  );
}

// The element(s) an aria-describedby attribute points at must exist and say why.
function description(el: HTMLElement): string {
  const ids = (el.getAttribute("aria-describedby") ?? "").split(" ").filter(Boolean);
  expect(ids.length, "aria-describedby").toBeGreaterThan(0);
  return ids
    .map((id) => {
      const target = document.getElementById(id);
      expect(target, `#${id}`).not.toBeNull();
      return target!.textContent ?? "";
    })
    .join(" ");
}

afterEach(() => vi.restoreAllMocks());

describe("switch helpers", () => {
  it("labels every /admin/system switch and every registry switch", () => {
    expect([...SWITCH_NAMES].sort()).toEqual(Object.keys(system().switches).sort());
    for (const w of WIRING) for (const s of w.requiredSwitches ?? []) expect(SWITCH_LABELS[s], w.id).toBeDefined();
    for (const s of Object.values(FEATURE_ERROR_SWITCH)) expect(SWITCH_NAMES).toContain(s);
  });

  it("maps only feature 403 codes to switches", () => {
    expect(featureSwitchFor(new ApiError("x", 403, "model_management_disabled"))).toBe("allow_model_management");
    expect(featureSwitchFor(new ApiError("x", 403, "downloads_disabled"))).toBe("allow_network_downloads");
    expect(featureSwitchFor(new ApiError("x", 403, "diagnostics_disabled"))).toBe("diagnostics_enabled");
    // Authorization, unknown 403s, other statuses, and inherited keys are not feature denials.
    expect(featureSwitchFor(new ApiError("x", 403, "operator_role_required"))).toBeNull();
    expect(featureSwitchFor(new ApiError("x", 403, "something_else"))).toBeNull();
    expect(featureSwitchFor(new ApiError("x", 403))).toBeNull();
    expect(featureSwitchFor(new ApiError("x", 401, "model_management_disabled"))).toBeNull();
    expect(featureSwitchFor(new ApiError("x", 403, "toString"))).toBeNull();
    expect(featureSwitchFor(new Error("model_management_disabled"))).toBeNull();
  });

  it("names every off switch in the reason", () => {
    expect(switchReason(["allow_model_management"])).toBe(
      "Model management is disabled (allow_model_management=false).",
    );
    expect(switchReason(["allow_model_management", "allow_network_downloads"])).toBe(
      "Model management and network model downloads are disabled (allow_model_management=false, allow_network_downloads=false).",
    );
    expect(switchReason([])).toBe("");
  });

  it("keeps authorization failures distinct from disabled features", () => {
    expect(describeActionError(new ApiError("no", 403, "downloads_disabled"))).toMatch(
      /^Network model downloads is disabled \(allow_network_downloads=false\)/,
    );
    expect(describeActionError(new ApiError("client key", 403, "operator_role_required"))).toMatch(
      /^Operator access required/,
    );
    expect(describeActionError(new ApiError("forbidden by policy", 403, "ip_not_allowed"))).toBe(
      "forbidden by policy",
    );
  });
});

function Probe({ name }: { name: SwitchName }): JSX.Element {
  const sys = useSystem();
  const v = sys.switchState(name);
  return (
    <div>
      <span data-testid="status">{sys.status}</span>
      <span data-testid="value">{v === null ? "unknown" : v ? "on" : "off"}</span>
      <span data-testid="stale">{String(sys.stale)}</span>
      <button onClick={sys.refresh}>refresh</button>
      <button onClick={() => sys.reportDenied(name)}>deny</button>
    </div>
  );
}

describe("SystemProvider", () => {
  it("is unknown without a provider", () => {
    render(<Probe name="allow_model_management" />);
    expect(screen.getByTestId("status")).toHaveTextContent("absent");
    expect(screen.getByTestId("value")).toHaveTextContent("unknown");
  });

  it("fetches once, then reports known state", async () => {
    const spy = vi.spyOn(api, "system").mockResolvedValue(system({ allow_model_management: false }));
    render(
      <SystemProvider>
        <Probe name="allow_model_management" />
        <Probe name="allow_network_downloads" />
      </SystemProvider>,
    );
    expect(screen.getAllByTestId("status")[0]).toHaveTextContent("loading");
    expect(screen.getAllByTestId("value")[0]).toHaveTextContent("unknown");
    await waitFor(() => expect(screen.getAllByTestId("status")[0]).toHaveTextContent("ready"));
    expect(screen.getAllByTestId("value").map((e) => e.textContent)).toEqual(["off", "on"]);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("keeps the last known state, marked stale, when a refresh fails", async () => {
    const spy = vi.spyOn(api, "system").mockResolvedValueOnce(system());
    render(
      <SystemProvider>
        <Probe name="allow_model_management" />
      </SystemProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("value")).toHaveTextContent("on"));
    spy.mockRejectedValueOnce(new ApiError("boom", 500));
    await userEvent.click(screen.getByRole("button", { name: "refresh" }));
    await waitFor(() => expect(screen.getByTestId("stale")).toHaveTextContent("true"));
    expect(screen.getByTestId("value")).toHaveTextContent("on");
  });

  it("ignores a late response from an earlier request", async () => {
    const first = deferred<SystemInfo>();
    const second = deferred<SystemInfo>();
    vi.spyOn(api, "system").mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(
      <SystemProvider>
        <Probe name="allow_model_management" />
      </SystemProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "refresh" }));
    await act(async () => second.resolve(system({ allow_model_management: false })));
    expect(screen.getByTestId("value")).toHaveTextContent("off");
    await act(async () => first.resolve(system({ allow_model_management: true })));
    expect(screen.getByTestId("value")).toHaveTextContent("off");
  });

  it("discards a response that arrives after the provider (session) is gone", async () => {
    const pending = deferred<SystemInfo>();
    vi.spyOn(api, "system").mockReturnValueOnce(pending.promise).mockResolvedValueOnce(system());
    const { unmount } = render(
      <SystemProvider key="a">
        <Probe name="allow_model_management" />
      </SystemProvider>,
    );
    unmount();
    render(
      <SystemProvider key="b">
        <Probe name="allow_model_management" />
      </SystemProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("value")).toHaveTextContent("on"));
    await act(async () => pending.resolve(system({ allow_model_management: false })));
    expect(screen.getByTestId("value")).toHaveTextContent("on");
  });

  it("marks a refused switch off at once and refreshes", async () => {
    const spy = vi.spyOn(api, "system").mockResolvedValueOnce(system());
    render(
      <SystemProvider>
        <Probe name="allow_model_management" />
      </SystemProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("value")).toHaveTextContent("on"));
    const refreshed = deferred<SystemInfo>();
    spy.mockReturnValueOnce(refreshed.promise);
    await userEvent.click(screen.getByRole("button", { name: "deny" }));
    // The stale "on" is not shown while the server's denial contradicts it.
    expect(screen.getByTestId("value")).toHaveTextContent("off");
    expect(spy).toHaveBeenCalledTimes(2);
    await act(async () => refreshed.resolve(system({ allow_model_management: false })));
    expect(screen.getByTestId("value")).toHaveTextContent("off");
  });

  it("refreshes when the browser comes back online", async () => {
    const spy = vi.spyOn(api, "system").mockRejectedValueOnce(new ApiError("network error: down", 0));
    render(
      <SystemProvider>
        <Probe name="allow_model_management" />
      </SystemProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("status")).toHaveTextContent("error"));
    spy.mockResolvedValueOnce(system());
    act(() => {
      window.dispatchEvent(new Event("online"));
    });
    await waitFor(() => expect(screen.getByTestId("value")).toHaveTextContent("on"));
  });
});

describe("Models with feature switches", () => {
  it("disables management actions with a visible, associated reason; cancel stays usable", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: ROWS });
    vi.spyOn(api, "system").mockResolvedValue(system({ allow_model_management: false }));
    renderModels();
    await screen.findByText("ready", { selector: ".mono" });
    const notice = await screen.findByText(/Model management is disabled \(allow_model_management=false\)/, {
      selector: ".switch-notice span",
    });
    expect(notice).toBeVisible();

    const load = screen.getByRole("button", { name: /^Load/ });
    const unload = screen.getByRole("button", { name: /^Unload/ });
    const del = screen.getByRole("button", { name: "Delete ready" });
    for (const b of [load, unload, del]) {
      expect(b).toBeDisabled();
      expect(description(b)).toMatch(/allow_model_management=false/);
    }
    // Controls are explained, never hidden.
    expect(screen.getByRole("button", { name: "Delete ready" })).toBeVisible();
    const cancel = screen.getByRole("button", { name: /^Cancel/ });
    expect(cancel).toBeEnabled();
    expect(cancel).not.toHaveAttribute("aria-describedby");

    // The add form explains the switch for the selected mode, too.
    await userEvent.click(screen.getByRole("tab", { name: "Local file" }));
    const importBtn = screen.getByRole("button", { name: /^Import model/ });
    expect(importBtn).toBeDisabled();
    expect(description(importBtn)).toMatch(/Model management is disabled.*Importing models is unavailable/);
  });

  it("lists both switches for downloads when both are off", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: [] });
    vi.spyOn(api, "system").mockResolvedValue(
      system({ allow_model_management: false, allow_network_downloads: false }),
    );
    renderModels();
    const download = await screen.findByRole("button", { name: /^Download model/ });
    await waitFor(() => expect(download).toBeDisabled());
    expect(description(download)).toMatch(/allow_model_management=false, allow_network_downloads=false/);
  });

  it("restricts only downloads when network downloads alone are off", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: ROWS });
    vi.spyOn(api, "system").mockResolvedValue(system({ allow_network_downloads: false }));
    renderModels();
    await screen.findByText("ready", { selector: ".mono" });
    const download = screen.getByRole("button", { name: /^Download model/ });
    await waitFor(() => expect(download).toBeDisabled());
    expect(description(download)).toMatch(/^.*Network model downloads is disabled \(allow_network_downloads=false\)/);
    expect(screen.getByRole("button", { name: /^Load/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Delete ready" })).toBeEnabled();
    expect(document.querySelector(".switch-notice")).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: "Local file" }));
    expect(screen.getByRole("button", { name: /^Import model/ })).toBeEnabled();
  });

  it("says the status is unavailable (not enabled) after a failure, and retries", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: ROWS });
    const spy = vi.spyOn(api, "system").mockRejectedValueOnce(new ApiError("engine error", 500));
    renderModels();
    const notice = await screen.findByText(/Feature-switch status unavailable/);
    expect(notice).toHaveTextContent("engine error (HTTP 500)");
    // Unknown state leaves actions to the engine.
    expect(screen.getByRole("button", { name: /^Load/ })).toBeEnabled();

    spy.mockResolvedValueOnce(system({ allow_model_management: false }));
    await userEvent.click(within(notice.closest(".switch-notice") as HTMLElement).getByRole("button", { name: /Retry/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /^Load/ })).toBeDisabled());
    expect(screen.queryByText(/Feature-switch status unavailable/)).toBeNull();
  });

  it("refreshes switch state from the Models Refresh action", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: ROWS });
    const spy = vi.spyOn(api, "system").mockResolvedValueOnce(system());
    renderModels();
    await screen.findByText("ready", { selector: ".mono" });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    spy.mockResolvedValueOnce(system({ allow_model_management: false }));
    await userEvent.click(screen.getByRole("button", { name: /^Refresh/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /^Load/ })).toBeDisabled());
  });

  it("explains a feature denial, refreshes state, and does not keep a stale 'on'", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: ROWS });
    const spy = vi.spyOn(api, "system").mockResolvedValueOnce(system());
    vi.spyOn(api, "loadModelById").mockRejectedValue(
      new ApiError("model management is disabled by the operator", 403, "model_management_disabled"),
    );
    renderModels();
    await screen.findByText("ready", { selector: ".mono" });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    const refreshed = deferred<SystemInfo>();
    spy.mockReturnValueOnce(refreshed.promise);

    await userEvent.click(screen.getByRole("button", { name: /^Load/ }));
    expect(await screen.findByText(/The engine refused this action/)).toHaveTextContent(
      "Model management is disabled (allow_model_management=false).",
    );
    expect(spy).toHaveBeenCalledTimes(2);
    // Before the refresh answers, the denial already disables the other actions.
    expect(screen.getByRole("button", { name: "Delete ready" })).toBeDisabled();
    expect(description(screen.getByRole("button", { name: "Delete ready" }))).toMatch(/allow_model_management=false/);
    await act(async () => refreshed.resolve(system({ allow_model_management: false })));
    expect(screen.getByRole("button", { name: "Delete ready" })).toBeDisabled();
  });

  it("explains a denial that arrives while the switch state is still loading", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: ROWS });
    vi.spyOn(api, "system").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "loadModelById").mockRejectedValue(
      new ApiError("model management is disabled by the operator", 403, "model_management_disabled"),
    );
    renderModels();
    await screen.findByText("ready", { selector: ".mono" });
    await userEvent.click(screen.getByRole("button", { name: /^Load/ }));
    await screen.findByText(/The engine refused this action/);
    const del = screen.getByRole("button", { name: "Delete ready" });
    expect(del).toBeDisabled();
    // The explanation the button points at is rendered even though loading.
    expect(description(del)).toMatch(/allow_model_management=false/);
  });

  it("reports an authorization 403 as such, never as a disabled feature", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: ROWS });
    const spy = vi.spyOn(api, "system").mockResolvedValue(system());
    vi.spyOn(api, "loadModelById").mockRejectedValue(
      new ApiError("this key belongs to a client", 403, "operator_role_required"),
    );
    renderModels();
    await screen.findByText("ready", { selector: ".mono" });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByRole("button", { name: /^Load/ }));
    expect(await screen.findByText(/^Operator access required/)).toBeVisible();
    expect(screen.queryByText(/is disabled \(/)).toBeNull();
    expect(screen.getByRole("button", { name: /^Load/ })).toBeEnabled();
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("does not map an unknown 403 to a switch", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: ROWS });
    vi.spyOn(api, "system").mockResolvedValue(system());
    vi.spyOn(api, "unloadModelById").mockRejectedValue(new ApiError("blocked by policy", 403, "policy_denied"));
    renderModels();
    await screen.findByText("loaded", { selector: ".mono" });
    await userEvent.click(screen.getByRole("button", { name: /^Unload/ }));
    expect(await screen.findByText("blocked by policy")).toBeVisible();
    expect(screen.queryByText(/is disabled \(/)).toBeNull();
  });

  it("re-checks the switch after a delete confirmation opened", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: ROWS });
    const spy = vi.spyOn(api, "system").mockResolvedValueOnce(system());
    const del = vi.spyOn(api, "deleteModel").mockResolvedValue({ deleted: true, id: "m1" });
    renderModels();
    await screen.findByText("ready", { selector: ".mono" });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByRole("button", { name: "Delete ready" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete ready?" });

    // While the confirmation is open, the engine reports the switch as off.
    spy.mockResolvedValueOnce(system({ allow_model_management: false }));
    act(() => {
      window.dispatchEvent(new Event("online"));
    });
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText(/Loading, unloading and deleting/)).toBeInTheDocument());

    await userEvent.click(within(dialog).getByRole("button", { name: "Delete model" }));
    expect(del).not.toHaveBeenCalled();
    expect(await screen.findByText("Model management is disabled (allow_model_management=false).")).toBeVisible();
  });

  it("re-checks at submission and does not call the engine when a switch is known off", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: [] });
    vi.spyOn(api, "system").mockResolvedValue(system({ allow_model_management: false }));
    const imp = vi.spyOn(api, "importModel");
    renderModels();
    await userEvent.click(await screen.findByRole("tab", { name: "Local file" }));
    await userEvent.type(screen.getByLabelText("Local file path"), "/m/x.gguf");
    await waitFor(() => expect(screen.getByRole("button", { name: /^Import model/ })).toBeDisabled());
    // Neither the button nor Enter in the field reaches the engine.
    await userEvent.type(screen.getByLabelText("Local file path"), "{Enter}");
    expect(imp).not.toHaveBeenCalled();
  });
});
