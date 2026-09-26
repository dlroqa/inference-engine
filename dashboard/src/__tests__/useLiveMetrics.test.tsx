import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useLiveMetrics } from "../hooks/useLiveMetrics";
import { AuthScopeProvider } from "../hooks/useAuthScope";
import { api, ApiError, type MetricsSnapshot } from "../lib/api";

// The /metrics polling fallback (used when the socket is unavailable or rejected)
// hands session-ending failures to the shell and keeps others as "down".

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function noSocket() {
  vi.stubGlobal(
    "WebSocket",
    class {
      constructor() {
        throw new Error("socket unavailable");
      }
    },
  );
}

describe("useLiveMetrics polling fallback", () => {
  it("reports a 401 to the session instead of showing the connection as down", async () => {
    noSocket();
    vi.spyOn(api, "metrics").mockRejectedValue(new ApiError("invalid or revoked API key", 401));
    const report = vi.fn((_e: unknown) => true);
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AuthScopeProvider value={report}>{children}</AuthScopeProvider>
    );
    const { result } = renderHook(() => useLiveMetrics(60_000), { wrapper });
    await waitFor(() => expect(report).toHaveBeenCalledWith(expect.objectContaining({ status: 401 })));
    expect(result.current.status).toBe("polling");
  });

  it("marks the connection down for a server error", async () => {
    noSocket();
    vi.spyOn(api, "metrics").mockRejectedValue(new ApiError("engine error", 500));
    const report = vi.fn((_e: unknown) => false);
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AuthScopeProvider value={report}>{children}</AuthScopeProvider>
    );
    const { result } = renderHook(() => useLiveMetrics(60_000), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("down"));
    expect(report).toHaveBeenCalledTimes(1);
  });
});

// A controllable socket: tests open it, deliver frames and drop it.
class FakeSocket {
  static all: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  constructor() {
    FakeSocket.all.push(this);
  }
  close() {
    this.onclose?.();
  }
  /** A browser reports a failed socket as error, then close. */
  fail() {
    this.onerror?.();
    this.onclose?.();
  }
  open() {
    this.onopen?.();
  }
  frame(ts: number) {
    this.onmessage?.({ data: JSON.stringify({ ts }) });
  }
  static latest(): FakeSocket {
    return FakeSocket.all[FakeSocket.all.length - 1];
  }
}

// Every /metrics request is held until the test settles it.
function controlledPolls() {
  const polls: { resolve: (s: MetricsSnapshot) => void; reject: (e: unknown) => void }[] = [];
  vi.spyOn(api, "metrics").mockImplementation(
    () => new Promise<MetricsSnapshot>((resolve, reject) => polls.push({ resolve, reject })),
  );
  return polls;
}

const snap = (ts: number) => ({ ts }) as unknown as MetricsSnapshot;

function withSocket() {
  FakeSocket.all = [];
  vi.stubGlobal("WebSocket", FakeSocket);
}

// Starts with a live stream that has delivered frame ts=1.
function liveHook(pollMs = 60_000) {
  withSocket();
  const polls = controlledPolls();
  const hook = renderHook(() => useLiveMetrics(pollMs));
  act(() => {
    FakeSocket.latest().open();
    FakeSocket.latest().frame(1);
  });
  expect(hook.result.current).toMatchObject({ status: "live", fresh: true });
  return { ...hook, polls, first: FakeSocket.latest() };
}

describe("useLiveMetrics transport and freshness", () => {
  it("an open socket without a frame is not live and not fresh", () => {
    withSocket();
    controlledPolls();
    const { result } = renderHook(() => useLiveMetrics(60_000));
    act(() => FakeSocket.latest().open());
    expect(result.current).toMatchObject({ status: "connecting", snapshot: null, fresh: false });
    act(() => FakeSocket.latest().frame(1));
    expect(result.current).toMatchObject({ status: "live", fresh: true });
    expect(result.current.snapshot?.ts).toBe(1);
  });

  it("polling without a response yet has no snapshot and is not fresh", () => {
    noSocket();
    controlledPolls();
    const { result } = renderHook(() => useLiveMetrics(60_000));
    expect(result.current).toMatchObject({ status: "polling", snapshot: null, fresh: false });
  });

  it("after a disconnect, the REST fallback reports Polling (never Live) with fresh data", async () => {
    const { result, polls, first } = liveHook();
    act(() => first.close());
    // The fallback is being tried: not live, retained data not current.
    expect(result.current).toMatchObject({ status: "polling", fresh: false });
    expect(result.current.snapshot?.ts).toBe(1);
    await act(async () => polls[0].resolve(snap(2)));
    expect(result.current).toMatchObject({ status: "polling", fresh: true });
    expect(result.current.snapshot?.ts).toBe(2);
  });

  it("a failed fallback reports Down and keeps the retained data marked not current", async () => {
    const { result, polls, first } = liveHook();
    act(() => first.fail());
    await act(async () => polls[0].reject(new ApiError("engine error", 500)));
    expect(result.current).toMatchObject({ status: "down", fresh: false });
    expect(result.current.snapshot?.ts).toBe(1);
  });

  it("a recovered stream is not rolled back by an older poll that succeeds late", async () => {
    const { result, polls, first } = liveHook(20);
    act(() => first.close());
    // The retry socket opens and delivers before the fallback poll answers.
    await waitFor(() => expect(FakeSocket.all.length).toBe(2));
    act(() => {
      FakeSocket.latest().open();
      FakeSocket.latest().frame(5);
    });
    expect(result.current).toMatchObject({ status: "live", fresh: true });
    await act(async () => polls[0].resolve(snap(3)));
    expect(result.current).toMatchObject({ status: "live", fresh: true });
    expect(result.current.snapshot?.ts).toBe(5);
  });

  it("a recovered stream is not marked down by an older poll that fails late", async () => {
    const { result, polls, first } = liveHook(20);
    act(() => first.close());
    await waitFor(() => expect(FakeSocket.all.length).toBe(2));
    act(() => {
      FakeSocket.latest().open();
      FakeSocket.latest().frame(5);
    });
    await act(async () => polls[0].reject(new ApiError("engine error", 500)));
    expect(result.current).toMatchObject({ status: "live", fresh: true });
    expect(result.current.snapshot?.ts).toBe(5);
  });

  it("overlapping polls cannot roll back the latest accepted result", async () => {
    noSocket();
    const polls = controlledPolls();
    const { result } = renderHook(() => useLiveMetrics(20));
    await waitFor(() => expect(polls.length).toBeGreaterThanOrEqual(2));
    await act(async () => polls[1].resolve(snap(20)));
    expect(result.current.snapshot?.ts).toBe(20);
    await act(async () => polls[0].resolve(snap(10)));
    expect(result.current.snapshot?.ts).toBe(20);
    await act(async () => polls[0].reject(new ApiError("late", 500)));
    expect(result.current).toMatchObject({ status: "polling", fresh: true });
  });

  it("a failed reconnect does not spoil a REST fallback that is delivering", async () => {
    const { result, polls, first } = liveHook(20);
    act(() => first.close());
    await act(async () => polls[0].resolve(snap(2)));
    expect(result.current).toMatchObject({ status: "polling", fresh: true });
    await waitFor(() => expect(FakeSocket.all.length).toBe(2));
    act(() => FakeSocket.latest().fail());
    expect(result.current).toMatchObject({ status: "polling", fresh: true });
    expect(result.current.snapshot?.ts).toBe(2);
    // A reconnect that opens without a frame does not claim Live either.
    await waitFor(() => expect(FakeSocket.all.length).toBe(3));
    act(() => FakeSocket.latest().open());
    expect(result.current).toMatchObject({ status: "polling", fresh: true });
  });

  it("error followed by close is one loss: one fallback poll, one reconnect", async () => {
    const { polls, first } = liveHook(40);
    act(() => first.fail());
    expect(polls.length).toBe(1);
    await waitFor(() => expect(FakeSocket.all.length).toBe(2));
    await new Promise((r) => setTimeout(r, 30));
    expect(FakeSocket.all.length).toBe(2);
  });

  it("reports a poll's auth failure to the session even after the stream recovered", async () => {
    withSocket();
    const polls = controlledPolls();
    const report = vi.fn((_e: unknown) => true);
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AuthScopeProvider value={report}>{children}</AuthScopeProvider>
    );
    const { result } = renderHook(() => useLiveMetrics(20), { wrapper });
    act(() => FakeSocket.latest().fail());
    await waitFor(() => expect(FakeSocket.all.length).toBe(2));
    act(() => FakeSocket.latest().frame(7));
    await act(async () => polls[0].reject(new ApiError("invalid or revoked API key", 401)));
    expect(report).toHaveBeenCalledWith(expect.objectContaining({ status: 401 }));
    expect(result.current).toMatchObject({ status: "live", fresh: true });
  });

  it("ignores completions after unmount", async () => {
    noSocket();
    const polls = controlledPolls();
    const report = vi.fn((_e: unknown) => false);
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AuthScopeProvider value={report}>{children}</AuthScopeProvider>
    );
    const { result, unmount } = renderHook(() => useLiveMetrics(60_000), { wrapper });
    unmount();
    await act(async () => polls[0].reject(new ApiError("invalid or revoked API key", 401)));
    expect(report).not.toHaveBeenCalled();
    expect(result.current).toMatchObject({ status: "polling", snapshot: null });
  });
});
