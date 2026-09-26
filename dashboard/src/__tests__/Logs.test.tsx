import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Logs } from "../views/Logs";
import { api, ApiError, type LogEvent } from "../lib/api";

function logEvent(over: Partial<LogEvent>): LogEvent {
  return {
    ts: 1_700_000_000,
    level: "ERROR",
    logger: "engine.request",
    event: "request_error",
    request_id: "chatcmpl-abc",
    route: "/v1/chat/completions",
    model: "tiny",
    key_id: null,
    category: "backend",
    stage: "generation",
    detail: "generation failed",
    stacktrace: "Traceback…",
    ...over,
  };
}

afterEach(() => vi.restoreAllMocks());

describe("Logs view", () => {
  it("shows a loading state before data resolves", () => {
    vi.spyOn(api, "logs").mockReturnValue(new Promise(() => {}));
    render(<Logs />);
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
  });

  it("renders an error state with retry", async () => {
    vi.spyOn(api, "logs").mockRejectedValue(new ApiError("nope", 500));
    render(<Logs />);
    expect(await screen.findByRole("alert")).toHaveTextContent("nope");
  });

  it("renders populated rows with category badges", async () => {
    vi.spyOn(api, "logs").mockResolvedValue({ events: [logEvent({})] });
    render(<Logs />);
    expect(await screen.findByText("request_error")).toBeInTheDocument();
    expect(screen.getByText("backend")).toBeInTheDocument();
    expect(screen.getByText("generation")).toBeInTheDocument();
  });

  it("filters rows by the query box", async () => {
    vi.spyOn(api, "logs").mockResolvedValue({
      events: [
        logEvent({ request_id: "req-keepme", event: "a", category: "model" }),
        logEvent({ request_id: "req-other", event: "b", category: "backend" }),
      ],
    });
    render(<Logs />);
    await screen.findByText("a");
    await userEvent.type(screen.getByLabelText("Filter"), "keepme");
    expect(screen.getByText("a")).toBeInTheDocument();
    expect(screen.queryByText("b")).not.toBeInTheDocument();
  });

  it("shows an empty state when there are no events", async () => {
    vi.spyOn(api, "logs").mockResolvedValue({ events: [] });
    render(<Logs />);
    expect(await screen.findByText("No matching log events.")).toBeInTheDocument();
  });

  it("reloads when the level filter changes", async () => {
    const spy = vi.spyOn(api, "logs").mockResolvedValue({ events: [] });
    render(<Logs />);
    await waitFor(() => expect(spy).toHaveBeenCalled());
    await userEvent.selectOptions(screen.getByLabelText("Level"), "ERROR");
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ limit: 200, level: "ERROR" }));
  });

  it("filters by request id on the server and can clear it", async () => {
    const spy = vi.spyOn(api, "logs").mockResolvedValue({ events: [logEvent({ event: "old-one" })] });
    const onNavigate = vi.fn();
    render(<Logs requestId="chatcmpl-abc" onNavigate={onNavigate} />);
    await screen.findByText("old-one");
    expect(spy).toHaveBeenCalledWith({ limit: 200, request_id: "chatcmpl-abc" });
    expect(screen.getByRole("status")).toHaveTextContent("chatcmpl-abc");
    await userEvent.click(screen.getByRole("button", { name: "Clear request filter" }));
    expect(onNavigate).toHaveBeenCalledWith("logs");
  });
});
