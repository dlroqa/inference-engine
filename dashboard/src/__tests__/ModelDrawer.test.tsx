import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Models } from "../views/Models";
import { AuthScopeProvider } from "../hooks/useAuthScope";
import { buildHash, parseHash } from "../hooks/useHashRoute";
import { api, ApiError, type ModelInfo } from "../lib/api";

const model = (over: Partial<ModelInfo> = {}): ModelInfo => ({
  id: "m1",
  name: "tiny",
  filename: "tiny.gguf",
  source_type: "url",
  source_ref: "https://cdn.example.com/tiny.gguf?token=***",
  sha256: "2e8040ceae7815abe0dcb3540b9995eaa1fa0d2ca9e797d0a635ae4433c68c2d",
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

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const drawer = () => screen.getByRole("dialog");

beforeEach(() => {
  vi.spyOn(api, "listModels").mockResolvedValue({ models: [model()] });
});

afterEach(() => vi.restoreAllMocks());

describe("Model details drawer", () => {
  it("opens from a row's Details button through the shell's navigation", async () => {
    const nav = vi.fn();
    render(<Models onNavigate={nav} />);
    await userEvent.click(await screen.findByRole("button", { name: "Details for tiny" }));
    expect(nav).toHaveBeenCalledWith("models", { modelId: "m1", drawer: "open" });
  });

  it("shows loading, then the model's details, and closes through the shell", async () => {
    const get = deferred<ModelInfo>();
    vi.spyOn(api, "getModel").mockReturnValue(get.promise);
    const nav = vi.fn();
    render(<Models modelId="m1" onNavigate={nav} />);
    expect(within(drawer()).getByRole("status", { name: "Loading" })).toBeInTheDocument();
    // Focus moves into the drawer, onto its first control.
    expect(within(drawer()).getByRole("button", { name: "Close" })).toHaveFocus();
    await act(async () => get.resolve(model()));
    const details = within(drawer()).getByTestId("model-details");
    expect(drawer()).toHaveAccessibleName("Model details: tiny");
    expect(within(details).getByTestId("model-sha256")).toHaveTextContent(model().sha256!);
    expect(within(details).getByText("Compatibility").nextSibling).toHaveTextContent("ok should run");
    expect(within(details).getByText("Context length").nextSibling).toHaveTextContent("4,096");
    await userEvent.click(within(drawer()).getByRole("button", { name: "Close" }));
    expect(nav).toHaveBeenLastCalledWith("models", { drawer: "close" });
    await userEvent.keyboard("{Escape}");
    expect(nav).toHaveBeenCalledTimes(2);
  });

  it("never renders a local import's path, and shows remote sources as text only", async () => {
    vi.spyOn(api, "getModel").mockResolvedValue(
      model({ source_type: "import", source_ref: "/home/operator/secret-dir/tiny.gguf" }),
    );
    const { unmount } = render(<Models modelId="m1" />);
    const source = await screen.findByTestId("model-source");
    expect(source).toHaveTextContent("Local import (path not shown)");
    expect(drawer().textContent).not.toContain("/home/operator/secret-dir");
    unmount();

    vi.mocked(api.getModel).mockResolvedValue(model());
    render(<Models modelId="m1" />);
    const remote = await screen.findByTestId("model-source");
    expect(remote).toHaveTextContent("URL: https://cdn.example.com/tiny.gguf?token=***");
    expect(within(drawer()).queryAllByRole("link").filter((a) => a.closest("[data-testid=model-details]"))).toEqual([]);
  });

  it("shows the error text as text", async () => {
    vi.spyOn(api, "getModel").mockResolvedValue(
      model({ status: "error", error: "HTTP 403 fetching https://cdn.example.com/m.gguf?token=***" }),
    );
    render(<Models modelId="m1" />);
    const details = await screen.findByTestId("model-details");
    expect(within(details).getByText("Error").nextSibling).toHaveTextContent(
      "HTTP 403 fetching https://cdn.example.com/m.gguf?token=***",
    );
    expect(within(details).queryByRole("link")).toBeNull();
  });

  it("shows not found for a 404", async () => {
    vi.spyOn(api, "getModel").mockRejectedValue(new ApiError("model 'gone' not found", 404, "model_not_found"));
    render(<Models modelId="gone" />);
    expect(await within(drawer()).findByText(/no longer exists/)).toBeInTheDocument();
    expect(within(drawer()).getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("treats a 404 without the model_not_found code as an ordinary error, with Retry", async () => {
    vi.spyOn(api, "getModel")
      .mockRejectedValueOnce(new ApiError("request failed (404)", 404))
      .mockRejectedValueOnce(new ApiError("no route", 404, "not_found"))
      .mockResolvedValueOnce(model());
    render(<Models modelId="m1" />);
    let alert = await within(drawer()).findByRole("alert");
    expect(alert).toHaveTextContent("request failed (404)");
    expect(within(drawer()).queryByText(/no longer exists/)).toBeNull();
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    alert = await within(drawer()).findByText(/no route/);
    expect(within(drawer()).queryByText(/no longer exists/)).toBeNull();
    await userEvent.click(within(drawer()).getByRole("button", { name: "Retry" }));
    expect(await within(drawer()).findByTestId("model-details")).toBeInTheDocument();
  });

  it("keeps the last details, stale, when a background refresh gets an unrelated 404", async () => {
    vi.spyOn(api, "getModel")
      .mockResolvedValueOnce(model())
      .mockRejectedValueOnce(new ApiError("request failed (404)", 404));
    render(<Models modelId="m1" />);
    await within(drawer()).findByTestId("model-details");
    vi.mocked(api.listModels).mockResolvedValue({ models: [model({ loaded: true })] });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    const stale = await within(drawer()).findByRole("alert");
    expect(stale).toHaveTextContent("Could not refresh these details (request failed (404))");
    expect(within(drawer()).getByTestId("model-details")).toBeInTheDocument();
    expect(within(drawer()).queryByText(/no longer exists/)).toBeNull();
  });

  it("shows not found on a direct link's first load for a genuine model_not_found", async () => {
    vi.spyOn(api, "listModels").mockResolvedValue({ models: [model()] });
    vi.spyOn(api, "getModel").mockRejectedValue(new ApiError("model 'x' not found", 404, "model_not_found"));
    render(<Models modelId="x" />);
    expect(await within(drawer()).findByText(/no longer exists/)).toBeInTheDocument();
    expect(within(drawer()).queryByTestId("model-details")).toBeNull();
  });

  it("shows a server error with Retry, and Retry recovers", async () => {
    vi.spyOn(api, "getModel")
      .mockRejectedValueOnce(new ApiError("engine error", 500))
      .mockResolvedValueOnce(model());
    render(<Models modelId="m1" />);
    const alert = await within(drawer()).findByRole("alert");
    expect(alert).toHaveTextContent("engine error");
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await within(drawer()).findByTestId("model-details")).toBeInTheDocument();
  });

  it("ignores a late response for a previous id (rapid id change)", async () => {
    const first = deferred<ModelInfo>();
    const second = deferred<ModelInfo>();
    vi.spyOn(api, "getModel").mockImplementation((id) => (id === "m1" ? first.promise : second.promise));
    const { rerender } = render(<Models modelId="m1" />);
    rerender(<Models modelId="m2" />);
    await act(async () => second.resolve(model({ id: "m2", name: "second" })));
    await act(async () => first.resolve(model({ id: "m1", name: "first-late" })));
    expect(drawer()).toHaveAccessibleName("Model details: second");
    expect(drawer().textContent).not.toContain("first-late");
  });

  it("keeps the newest response when an earlier one for the same id arrives last (Retry race)", async () => {
    const older = deferred<ModelInfo>();
    const newer = deferred<ModelInfo>();
    vi.spyOn(api, "getModel")
      .mockResolvedValueOnce(model())
      .mockRejectedValueOnce(new ApiError("engine error", 500))
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    render(<Models modelId="m1" />);
    await within(drawer()).findByTestId("model-details");
    // A failed background refresh leaves the stale details with Retry.
    vi.mocked(api.listModels).mockResolvedValue({ models: [model({ loaded: true })] });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    const retry = await within(drawer()).findByRole("button", { name: "Retry" });
    // Two Retries for the same id; the first one's response arrives last.
    fireEvent.click(retry);
    fireEvent.click(within(drawer()).getByRole("button", { name: "Retry" }));
    await act(async () => newer.resolve(model({ name: "newest" })));
    await act(async () => older.resolve(model({ name: "older" })));
    expect(drawer()).toHaveAccessibleName("Model details: newest");
    expect(within(drawer()).queryByRole("alert")).toBeNull();
  });

  it("drops a response that arrives after the drawer closes", async () => {
    const get = deferred<ModelInfo>();
    vi.spyOn(api, "getModel").mockReturnValue(get.promise);
    const { rerender } = render(<Models modelId="m1" />);
    rerender(<Models modelId={null} />);
    await act(async () => get.resolve(model()));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("hands an auth failure to the session instead of showing an error", async () => {
    vi.spyOn(api, "getModel").mockRejectedValue(new ApiError("invalid or revoked API key", 401));
    const report = vi.fn((_e: unknown) => true);
    render(
      <AuthScopeProvider value={report}>
        <Models modelId="m1" />
      </AuthScopeProvider>,
    );
    await waitFor(() => expect(report).toHaveBeenCalledWith(expect.objectContaining({ status: 401 })));
    expect(within(drawer()).queryByRole("alert")).toBeNull();
  });

  it("refreshes when the list shows a changed row, including compatibility", async () => {
    vi.spyOn(api, "getModel")
      .mockResolvedValueOnce(model())
      .mockResolvedValueOnce(model({ compat: { status: "too_large", reason: "needs 8 GB" } }));
    render(<Models modelId="m1" />);
    await within(drawer()).findByTestId("model-details");
    expect(api.getModel).toHaveBeenCalledTimes(1);
    // An unchanged list refresh does not refetch.
    vi.mocked(api.listModels).mockResolvedValue({ models: [model()] });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(vi.mocked(api.listModels).mock.calls.length).toBeGreaterThanOrEqual(2));
    await act(async () => {}); // let the list result settle
    expect(api.getModel).toHaveBeenCalledTimes(1);
    // A compatibility change in the list does.
    vi.mocked(api.listModels).mockResolvedValue({
      models: [model({ compat: { status: "too_large", reason: "needs 8 GB" } })],
    });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(api.getModel).toHaveBeenCalledTimes(2));
    expect(await within(drawer()).findByText("needs 8 GB")).toBeInTheDocument();
  });

  it("shows not found once a successful list refresh no longer has the model", async () => {
    vi.spyOn(api, "getModel")
      .mockResolvedValueOnce(model())
      .mockRejectedValueOnce(new ApiError("not found", 404, "model_not_found"));
    render(<Models modelId="m1" />);
    await within(drawer()).findByTestId("model-details");
    // A failing list refresh proves nothing: no refetch.
    vi.mocked(api.listModels).mockRejectedValueOnce(new ApiError("engine error", 500));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(vi.mocked(api.listModels).mock.calls.length).toBeGreaterThanOrEqual(2));
    await act(async () => {}); // let the list result settle
    expect(api.getModel).toHaveBeenCalledTimes(1);
    // A successful refresh without the row confirms it is gone.
    vi.mocked(api.listModels).mockResolvedValue({ models: [] });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await within(drawer()).findByText(/no longer exists/)).toBeInTheDocument();
    expect(within(drawer()).queryByTestId("model-details")).toBeNull();
  });

  it("keeps the last details, marked stale, when a background refresh fails; Retry recovers", async () => {
    vi.spyOn(api, "getModel")
      .mockResolvedValueOnce(model())
      .mockRejectedValueOnce(new ApiError("engine error", 500))
      .mockResolvedValueOnce(model({ status: "ready", loaded: true }));
    render(<Models modelId="m1" />);
    await within(drawer()).findByTestId("model-details");
    vi.mocked(api.listModels).mockResolvedValue({ models: [model({ loaded: true })] });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    const stale = await within(drawer()).findByRole("alert");
    expect(stale).toHaveTextContent("Could not refresh these details (engine error)");
    expect(within(drawer()).getByTestId("model-details")).toBeInTheDocument();
    await userEvent.click(within(stale).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(within(drawer()).queryByRole("alert")).toBeNull());
    expect(within(drawer()).getByText("loaded")).toBeInTheDocument();
  });

  it("copies the checksum and reports a refused clipboard truthfully", async () => {
    vi.spyOn(api, "getModel").mockResolvedValue(model());
    const writeText = vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error("denied"));
    const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    try {
      render(<Models modelId="m1" />);
      await within(drawer()).findByTestId("model-details");
      const copy = within(drawer()).getByRole("button", { name: "Copy checksum" });
      fireEvent.click(copy);
      expect(await within(drawer()).findByText("Checksum copied to the clipboard.")).toBeInTheDocument();
      expect(writeText).toHaveBeenCalledWith(model().sha256);
      fireEvent.click(copy);
      expect(await within(drawer()).findByText(/Could not copy/)).toBeInTheDocument();
      expect(drawer().textContent).not.toMatch(/secret|cannot be shown again/i);
    } finally {
      if (original) Object.defineProperty(navigator, "clipboard", original);
      else delete (navigator as { clipboard?: unknown }).clipboard;
    }
  });

  it("Escape closes an open hint popover first, then the drawer", async () => {
    vi.spyOn(api, "getModel").mockResolvedValue(model());
    const nav = vi.fn();
    render(<Models modelId="m1" onNavigate={nav} />);
    await within(drawer()).findByTestId("model-details");
    await userEvent.click(within(drawer()).getByRole("button", { name: "How this works: Model details" }));
    expect(within(drawer()).getByRole("group")).toHaveTextContent("GET /admin/models/{model_id}");
    await userEvent.keyboard("{Escape}");
    expect(within(drawer()).queryByRole("group")).toBeNull();
    expect(nav).not.toHaveBeenCalled();
    await userEvent.keyboard("{Escape}");
    expect(nav).toHaveBeenCalledWith("models", { drawer: "close" });
  });
});

describe("model id encoding", () => {
  it("round-trips an id through the hash router without double encoding", () => {
    const id = "m 1/ü%";
    const hash = buildHash("models", { segments: [id] });
    expect(hash).toBe("#/models/m%201%2F%C3%BC%25");
    expect(parseHash(hash).segments).toEqual([id]);
  });

  it("encodes an id exactly once in API URLs", async () => {
    const fetchSpy = vi.fn(async () => new Response(JSON.stringify(model()), { status: 200 }));
    vi.stubGlobal("fetch", fetchSpy);
    try {
      await api.getModel("m 1/ü%");
      await api.loadModelById("m 1/ü%").catch(() => undefined);
      expect(fetchSpy.mock.calls.map((c) => (c as unknown[])[0])).toEqual([
        "/admin/models/m%201%2F%C3%BC%25",
        "/admin/models/m%201%2F%C3%BC%25/load",
      ]);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
