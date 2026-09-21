import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Models } from "../views/Models";
import { api, ApiError, type ModelInfo } from "../lib/api";

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

afterEach(() => vi.restoreAllMocks());

describe("Models view", () => {
  it("shows a loading state", () => {
    vi.spyOn(api, "listModels").mockReturnValue(new Promise(() => {}));
    render(<Models />);
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: [] });
    render(<Models />);
    expect(await screen.findByText(/No models yet/)).toBeInTheDocument();
  });

  it("shows an error state", async () => {
    vi.spyOn(api, "listModels").mockRejectedValue(new ApiError("down", 500));
    render(<Models />);
    expect(await screen.findByRole("alert")).toHaveTextContent("down");
  });

  it("renders a ready model with load/delete and compat badge", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({
      models: [model({ compat: { status: "needs_backend", reason: "no AVX2" } })],
    });
    render(<Models />);
    expect(await screen.findByText("tiny")).toBeInTheDocument();
    expect(screen.getByText(/llama · Q4_K_M · 1000 B/)).toBeInTheDocument();
    expect(screen.getByText("needs_backend")).toBeInTheDocument();
    expect(screen.getByText("no AVX2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Load/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Delete tiny/ })).toBeInTheDocument();
  });

  it("shows download progress and a cancel action", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({
      models: [model({ status: "downloading", downloaded_bytes: 500, progress: 0.5 })],
    });
    render(<Models />);
    await screen.findByText("tiny");
    const meter = screen.getByRole("meter", { name: "Download progress" });
    expect(meter).toHaveAttribute("aria-valuenow", "50");
    expect(screen.getByRole("button", { name: /Cancel/ })).toBeInTheDocument();
  });

  it("downloads from a URL via the add form", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: [] });
    const dl = vi.spyOn(api, "downloadModel").mockResolvedValue(model({ status: "downloading" }));
    render(<Models />);
    await screen.findByText(/No models yet/);
    await userEvent.click(screen.getByRole("tab", { name: "URL" }));
    await userEvent.type(screen.getByLabelText("GGUF URL"), "http://x/m-Q4_K_M.gguf");
    await userEvent.click(screen.getByRole("button", { name: /Download model/ }));
    await waitFor(() =>
      expect(dl).toHaveBeenCalledWith(
        expect.objectContaining({ source_type: "url", url: "http://x/m-Q4_K_M.gguf" }),
      ),
    );
  });

  it("imports a local file via the add form", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: [] });
    const imp = vi.spyOn(api, "importModel").mockResolvedValue(model());
    render(<Models />);
    await screen.findByText(/No models yet/);
    await userEvent.click(screen.getByRole("tab", { name: "Local file" }));
    await userEvent.type(screen.getByLabelText("Local file path"), "/models/x.gguf");
    await userEvent.click(screen.getByRole("button", { name: /Import model/ }));
    await waitFor(() => expect(imp).toHaveBeenCalledWith("/models/x.gguf", null));
  });

  it("loads a ready model", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: [model()] });
    const load = vi.spyOn(api, "loadModelById").mockResolvedValue({ result: "loaded", model: model() });
    render(<Models />);
    await screen.findByText("tiny");
    await userEvent.click(screen.getByRole("button", { name: /^Load/ }));
    await waitFor(() => expect(load).toHaveBeenCalledWith("m1"));
  });

  it("unloads a loaded model", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: [model({ loaded: true, active: true })] });
    const unload = vi
      .spyOn(api, "unloadModelById")
      .mockResolvedValue({ result: "unloaded", model: model() });
    render(<Models />);
    await screen.findByText("tiny");
    await userEvent.click(screen.getByRole("button", { name: /Unload/ }));
    await waitFor(() => expect(unload).toHaveBeenCalledWith("m1"));
  });
});
