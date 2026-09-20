import { useCallback, useEffect, useState } from "react";

export type AsyncStatus = "loading" | "ready" | "error";

export interface AsyncState<T> {
  status: AsyncStatus;
  data: T | null;
  error: string | null;
  reload: () => void;
}

// Fetch-on-mount with a manual reload. Keeps the last good data across reloads
// so a transient error does not blank the view.
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [status, setStatus] = useState<AsyncStatus>("loading");
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(() => {
    let cancelled = false;
    setStatus((prev) => (data === null ? "loading" : prev));
    fn()
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
        setStatus("ready");
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setError(e.message);
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => run(), [run]);

  return { status, data, error, reload: run };
}
