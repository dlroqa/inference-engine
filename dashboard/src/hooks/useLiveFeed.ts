import { useEffect, useRef, useState } from "react";
import { wsUrl, type FeedEvent } from "../lib/api";
import type { ConnStatus } from "./useLiveMetrics";

export interface LiveFeed {
  events: FeedEvent[];
  status: ConnStatus;
}

// Streams the inference live feed over /ws/feed, keeping the most recent `cap`
// events. There is no REST equivalent for the ephemeral feed, so when the socket
// is unavailable the status is surfaced (the UI shows a reconnecting notice).
export function useLiveFeed(cap = 60): LiveFeed {
  const [events, setEvents] = useState<FeedEvent[]>([]);
  const [status, setStatus] = useState<ConnStatus>("connecting");
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedRef = useRef(false);

  useEffect(() => {
    closedRef.current = false;

    const connect = () => {
      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl("/ws/feed"));
      } catch {
        setStatus("down");
        retryRef.current = setTimeout(connect, 3000);
        return;
      }
      wsRef.current = ws;
      ws.onopen = () => setStatus("live");
      ws.onmessage = (ev) => {
        try {
          const event = JSON.parse(ev.data) as FeedEvent;
          setEvents((prev) => [...prev.slice(-(cap - 1)), event]);
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onerror = () => setStatus("down");
      ws.onclose = () => {
        wsRef.current = null;
        if (closedRef.current) return;
        setStatus("down");
        retryRef.current = setTimeout(connect, 3000);
      };
    };

    connect();

    return () => {
      closedRef.current = true;
      if (retryRef.current) clearTimeout(retryRef.current);
      wsRef.current?.close();
    };
  }, [cap]);

  return { events, status };
}
