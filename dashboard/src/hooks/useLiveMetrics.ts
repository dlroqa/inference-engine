import { useEffect, useRef, useState } from "react";
import { api, wsUrl, type MetricsSnapshot } from "../lib/api";
import { useAuthFailure } from "./useAuthScope";

export type ConnStatus = "connecting" | "live" | "polling" | "down";

export interface LiveMetrics {
  snapshot: MetricsSnapshot | null;
  status: ConnStatus;
}

// Streams metrics over /ws/metrics and falls back to REST polling of /metrics
// when the socket is unavailable, so the Overview keeps updating either way.
// A rejected socket (close 1008) falls back to polling; a polling 401 or
// operator_role_required then ends the session through the shell.
export function useLiveMetrics(pollMs = 3000): LiveMetrics {
  const [snapshot, setSnapshot] = useState<MetricsSnapshot | null>(null);
  const [status, setStatus] = useState<ConnStatus>("connecting");
  const wsRef = useRef<WebSocket | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedRef = useRef(false);
  const reportAuthFailure = useAuthFailure();
  const authRef = useRef(reportAuthFailure);
  authRef.current = reportAuthFailure;

  useEffect(() => {
    closedRef.current = false;

    const stopPolling = () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };

    const startPolling = () => {
      if (pollRef.current) return;
      setStatus((s) => (s === "live" ? s : "polling"));
      const tick = () =>
        api
          .metrics()
          .then((snap) => {
            if (closedRef.current) return;
            setSnapshot(snap);
            setStatus((s) => (s === "live" ? s : "polling"));
          })
          .catch((e: unknown) => {
            if (closedRef.current || authRef.current(e)) return;
            setStatus("down");
          });
      tick();
      pollRef.current = setInterval(tick, pollMs);
    };

    const connect = () => {
      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl("/ws/metrics"));
      } catch {
        startPolling();
        return;
      }
      wsRef.current = ws;
      ws.onopen = () => {
        stopPolling();
        setStatus("live");
      };
      ws.onmessage = (ev) => {
        try {
          setSnapshot(JSON.parse(ev.data));
          setStatus("live");
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onerror = () => {
        // Fall back to polling; onclose handles reconnect scheduling.
        startPolling();
      };
      ws.onclose = () => {
        wsRef.current = null;
        if (closedRef.current) return;
        startPolling();
        retryRef.current = setTimeout(connect, pollMs);
      };
    };

    connect();

    return () => {
      closedRef.current = true;
      stopPolling();
      if (retryRef.current) clearTimeout(retryRef.current);
      wsRef.current?.close();
    };
  }, [pollMs]);

  return { snapshot, status };
}
