import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Models } from "../views/Models";
import { api, ApiError, type ModelPanel } from "../lib/api";

const panel = (over: Partial<ModelPanel> = {}): ModelPanel => ({
  configured_id: "tiny",
  configured: true,
  state: "unloaded",
  loaded: false,
  model_id: null,
  ...over,
});

function overview(model: ModelPanel) {
  return {
    version: "0.0.0",
    ready: true,
    checks: {},
    model,
    metrics: {} as never,
  };
}

afterEach(() => vi.restoreAllMocks());

describe("Models view", () => {
  it("shows the configured model and enables Load when unloaded", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview(panel()));
    render(<Models />);
    expect(await screen.findByText("tiny")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Load model/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Unload/ })).toBeDisabled();
  });

  it("calls load and refreshes", async () => {
    const ov = vi
      .spyOn(api, "overview")
      .mockResolvedValueOnce(overview(panel()))
      .mockResolvedValueOnce(overview(panel({ state: "ready", loaded: true, model_id: "tiny" })));
    const load = vi.spyOn(api, "loadModel").mockResolvedValue({ result: "loaded", model: panel({ loaded: true }) });
    render(<Models />);
    await screen.findByText("tiny");
    await userEvent.click(screen.getByRole("button", { name: /Load model/ }));
    await waitFor(() => expect(load).toHaveBeenCalled());
    await waitFor(() => expect(ov).toHaveBeenCalledTimes(2));
  });

  it("surfaces a load failure", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview(panel()));
    vi.spyOn(api, "loadModel").mockRejectedValue(new ApiError("model failed to load", 503));
    render(<Models />);
    await screen.findByText("tiny");
    await userEvent.click(screen.getByRole("button", { name: /Load model/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("model failed to load");
  });

  it("disables Load when no model is configured", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview(panel({ configured: false })));
    render(<Models />);
    await screen.findByText("tiny");
    expect(screen.getByRole("button", { name: /Load model/ })).toBeDisabled();
    expect(screen.getByText(/Set/)).toBeInTheDocument();
  });

  it("shows an error state when overview fails", async () => {
    vi.spyOn(api, "overview").mockRejectedValue(new ApiError("unauthorized", 401));
    render(<Models />);
    expect(await screen.findByRole("alert")).toHaveTextContent("unauthorized");
  });
});
