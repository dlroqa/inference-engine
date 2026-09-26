import { useEffect, useRef, useState } from "react";
import { api, wsUrl, type MetricsSnapshot } from "../lib/api";
import { useAuthFailure } from "./useAuthScope";

export type ConnStatus = "connecting" | "live" | "polling" | "down";

export interface LiveMetrics {
  snapshot: MetricsSnapshot | null;
  status: ConnStatus;
  /**
   * True only while `snapshot` arrived since the last disconnect: set by a
   * valid frame or a successful poll, cleared when the transport is lost. An
   * open socket or a started poll alone does not make retained data fresh.
   */
  fresh: boolean;
}

// Streams metrics over /ws/metrics and falls back to REST polling of /metrics
// when the socket is unavailable, so the Overview keeps updating either way.
//
// Ownership (so no stale completion can roll state back):
// - Only the current socket's callbacks act; a replaced or closed socket's
//   late events are ignored. Error and close share one idempotent loss path
//   and schedule at most one reconnect.
// - Every poll carries the polling epoch it was issued in and a sequence
//   number. Stopping (or restarting) polling starts a new epoch, so requests
//   still in flight are ignored; within an epoch, a completion older than the
//   last one settled is ignored.
// - "live" is shown only after a valid frame. After a disconnect the status is
//   "polling" while the REST fallback is tried and once it delivers, and
//   "down" when it fails. A reconnecting socket that has opened but sent no
//   frame is "connecting" (or stays "polling" while polling delivers); its
//   first valid frame stops polling.
// - Auth failures from any poll go to the session's reporter, which decides
//   whether they belong to the current session; transport obsolescence only
//   stops a completion from changing this hook's state.
export function useLiveMetrics(pollMs = 3000): LiveMetrics {
  const [snapshot, setSnapshot] = useState<MetricsSnapshot | null>(null);
  const [status, setStatus] = useState<ConnStatus>("connecting");
  const [fresh, setFresh] = useState(false);
  const reportAuthFailure = useAuthFailure();
  const authRef = useRef(reportAuthFailure);
  authRef.current = reportAuthFailure;

  useEffect(() => {
    let closed = false;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let poll: ReturnType<typeof setInterval> | null = null;
    let epoch = 0;
    let issued = 0;
    let settled = 0;
    // Whether the running poll's latest settled request succeeded.
    let pollOk = false;

    const stopPolling = () => {
      if (poll) clearInterval(poll);
      poll = null;
      pollOk = false;
      epoch += 1;
    };

    const startPolling = () => {
      if (poll) return;
      epoch += 1;
      const mine = epoch;
      pollOk = false;
      setStatus("polling");
      const tick = () => {
        const seq = ++issued;
        const current = () => !closed && mine === epoch && seq > settled;
        api.metrics().then(
          (snap) => {
            if (!current()) return;
            settled = seq;
            pollOk = true;
            setSnapshot(snap);
            setFresh(true);
            setStatus("polling");
          },
          (e: unknown) => {
            if (closed || authRef.current(e)) return;
            if (!current()) return;
            settled = seq;
            pollOk = false;
            setFresh(false);
            setStatus("down");
          },
        );
      };
      poll = setInterval(tick, pollMs);
      tick();
    };

    const scheduleReconnect = () => {
      if (closed || retry) return;
      retry = setTimeout(() => {
        retry = null;
        connect();
      }, pollMs);
    };

    // The current socket is gone (error or close). Idempotent per socket.
    const lost = (sock: WebSocket | null) => {
      if (closed || (sock !== null && sock !== ws)) return;
      ws = null;
      // A failed reconnect must not spoil a REST fallback that is delivering.
      if (!(poll && pollOk)) setFresh(false);
      startPolling();
      scheduleReconnect();
    };

    const connect = () => {
      let sock: WebSocket;
      try {
        sock = new WebSocket(wsUrl("/ws/metrics"));
      } catch {
        lost(null);
        return;
      }
      ws = sock;
      sock.onopen = () => {
        if (closed || sock !== ws) return;
        // Open is not data: keep polling until the first valid frame.
        if (!(poll && pollOk)) setStatus("connecting");
      };
      sock.onmessage = (ev) => {
        if (closed || sock !== ws) return;
        let snap: MetricsSnapshot;
        try {
          snap = JSON.parse(ev.data);
        } catch {
          return; // ignore a malformed frame
        }
        stopPolling();
        setSnapshot(snap);
        setFresh(true);
        setStatus("live");
      };
      sock.onerror = () => lost(sock);
      sock.onclose = () => lost(sock);
    };

    connect();

    return () => {
      closed = true;
      stopPolling();
      if (retry) clearTimeout(retry);
      const sock = ws;
      ws = null;
      sock?.close();
    };
  }, [pollMs]);

  return { snapshot, status, fresh };
}
