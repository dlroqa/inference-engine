import type { ConnStatus } from "../hooks/useLiveMetrics";

// What a live-metrics surface may claim, given the transport state and whether
// the snapshot it holds arrived since the last disconnect or reconnect.
//
// Transport state alone never proves data is current: a socket can be open
// before its first frame, and polling can start before its first response.
// Without a snapshot nothing is shown as a number (no invented zeros, no
// conclusions about probes); with a retained snapshot the surface says it is
// not current until new data arrives.

export type LiveView =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "ready"; stale: false; polling: boolean }
  | { kind: "ready"; stale: true; reason: "disconnected" | "awaiting" };

export function liveView<T>(status: ConnStatus, snapshot: T | null, fresh: boolean): LiveView {
  if (snapshot === null) return status === "down" ? { kind: "unavailable" } : { kind: "loading" };
  if (fresh) return { kind: "ready", stale: false, polling: status === "polling" };
  return { kind: "ready", stale: true, reason: status === "down" ? "disconnected" : "awaiting" };
}

/** The notice shown beside retained (not current) metrics, or null. */
export function staleNotice(view: LiveView): string | null {
  if (view.kind !== "ready" || !view.stale) return null;
  return view.reason === "disconnected"
    ? "Disconnected — showing the last data received; reconnecting…"
    : "Reconnected — showing the last data received until new data arrives…";
}
