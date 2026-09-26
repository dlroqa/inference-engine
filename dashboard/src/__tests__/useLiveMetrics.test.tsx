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
  static latest(): FakeSocket {
    return FakeSocket.all[FakeSocket.all.length - 1];
  }
}

const snap = (ts: number) => ({ ts }) as unknown as MetricsSnapshot;

describe("useLiveMetrics freshness", () => {
  function withSocket() {
    FakeSocket.all = [];
    vi.stubGlobal("WebSocket", FakeSocket);
  }

  it("an open socket without a frame is not fresh data", () => {
    withSocket();
    vi.spyOn(api, "metrics").mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useLiveMetrics(60_000));
    act(() => FakeSocket.latest().onopen?.());
    expect(result.current).toMatchObject({ status: "live", snapshot: null, fresh: false });
    act(() => FakeSocket.latest().onmessage?.({ data: JSON.stringify(snap(1)) }));
    expect(result.current).toMatchObject({ status: "live", fresh: true });
    expect(result.current.snapshot?.ts).toBe(1);
  });

  it("polling without a response yet has no snapshot and is not fresh", () => {
    noSocket();
    vi.spyOn(api, "metrics").mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useLiveMetrics(60_000));
    expect(result.current).toMatchObject({ status: "polling", snapshot: null, fresh: false });
  });

  it("a dropped stream keeps the snapshot but marks it not fresh until new data arrives", async () => {
    withSocket();
    let resolvePoll: (s: MetricsSnapshot) => void = () => {};
    vi.spyOn(api, "metrics").mockImplementation(
      () => new Promise<MetricsSnapshot>((res) => (resolvePoll = res)),
    );
    const { result } = renderHook(() => useLiveMetrics(60_000));
    act(() => {
      FakeSocket.latest().onopen?.();
      FakeSocket.latest().onmessage?.({ data: JSON.stringify(snap(1)) });
    });
    expect(result.current.fresh).toBe(true);

    // Disconnect: the polling fallback starts, but has not answered yet.
    act(() => FakeSocket.latest().close());
    expect(result.current).toMatchObject({ status: "live", fresh: false });
    expect(result.current.snapshot?.ts).toBe(1);

    // Successful REST recovery makes the data current again.
    await act(async () => resolvePoll(snap(2)));
    expect(result.current.fresh).toBe(true);
    expect(result.current.snapshot?.ts).toBe(2);
  });

  it("reconnecting before the first new frame keeps retained data marked as not fresh", async () => {
    withSocket();
    vi.spyOn(api, "metrics").mockRejectedValue(new ApiError("engine error", 500));
    const { result } = renderHook(() => useLiveMetrics(20));
    act(() => {
      FakeSocket.latest().onopen?.();
      FakeSocket.latest().onmessage?.({ data: JSON.stringify(snap(1)) });
    });
    act(() => FakeSocket.latest().close());
    await waitFor(() => expect(result.current.status).toBe("down"));
    expect(result.current.fresh).toBe(false);

    // The retry socket opens: live again, but nothing new has arrived.
    await waitFor(() => expect(FakeSocket.all.length).toBe(2));
    act(() => FakeSocket.latest().onopen?.());
    expect(result.current).toMatchObject({ status: "live", fresh: false });
    expect(result.current.snapshot?.ts).toBe(1);
    act(() => FakeSocket.latest().onmessage?.({ data: JSON.stringify(snap(3)) }));
    expect(result.current).toMatchObject({ status: "live", fresh: true });
  });
});
