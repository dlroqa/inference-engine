import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useLiveMetrics } from "../hooks/useLiveMetrics";
import { AuthScopeProvider } from "../hooks/useAuthScope";
import { api, ApiError } from "../lib/api";

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
