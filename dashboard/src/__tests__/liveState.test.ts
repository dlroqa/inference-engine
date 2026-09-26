import { describe, expect, it } from "vitest";
import { liveView, staleNotice } from "../lib/liveState";

const snap = { ts: 1 };

describe("liveView", () => {
  it.each(["connecting", "live", "polling"] as const)("is loading without a snapshot (%s)", (status) => {
    expect(liveView(status, null, false)).toEqual({ kind: "loading" });
  });

  it("is unavailable when disconnected without a snapshot", () => {
    expect(liveView("down", null, false)).toEqual({ kind: "unavailable" });
  });

  it("marks a retained snapshot stale while disconnected", () => {
    const v = liveView("down", snap, false);
    expect(v).toEqual({ kind: "ready", stale: true, reason: "disconnected" });
    expect(staleNotice(v)).toMatch(/Disconnected/);
  });

  it.each(["connecting", "live", "polling"] as const)(
    "marks a retained snapshot as awaiting new data after reconnecting (%s)",
    (status) => {
      const v = liveView(status, snap, false);
      expect(v).toEqual({ kind: "ready", stale: true, reason: "awaiting" });
      expect(staleNotice(v)).toMatch(/until new data arrives/);
    },
  );

  it("is ready with fresh stream data", () => {
    const v = liveView("live", snap, true);
    expect(v).toEqual({ kind: "ready", stale: false, polling: false });
    expect(staleNotice(v)).toBeNull();
  });

  it("is ready with the polling state after a successful REST result", () => {
    expect(liveView("polling", snap, true)).toEqual({ kind: "ready", stale: false, polling: true });
  });
});
