import { useCallback, useEffect, useRef, useState } from "react";
import { getApiKey } from "../lib/api";

// Holds a value the engine returns exactly once (a client key's token, a
// webhook signing secret) for one owner (a client or an endpoint), and only for
// the session and view that asked for it.
//
// A response is kept only if its request is still this holder's current one,
// was not aborted, and the saved operator key is unchanged. Unmounting (view
// teardown, and the shell remounts the whole session on key change, forget, or
// auth loss) aborts the request and drops any late outcome; so does a change of
// owner. A dropped response is never shown anywhere: the server-side effect
// (the key or secret exists) stands, and the operator can revoke or rotate it.

export type OneTimeOutcome<T> =
  | { kind: "ok"; value: T }
  | { kind: "error"; error: unknown }
  | { kind: "stale" };

export interface OneTimeSecret<T> {
  /** The value to show, only while it belongs to the current owner. */
  value: T | null;
  busy: boolean;
  run: (call: (signal: AbortSignal) => Promise<T>) => Promise<OneTimeOutcome<T>>;
  /** Hide the value (the operator saved it). It cannot be shown again. */
  clear: () => void;
}

interface Pending {
  controller: AbortController;
  key: string | null;
  owner: string;
}

export function useOneTimeSecret<T>(owner: string): OneTimeSecret<T> {
  const [shown, setShown] = useState<{ owner: string; value: T } | null>(null);
  const [busy, setBusy] = useState(false);
  const current = useRef<Pending | null>(null);

  const invalidate = useCallback(() => {
    current.current?.controller.abort();
    current.current = null;
  }, []);

  // Teardown: abort and invalidate whatever is in flight.
  useEffect(() => invalidate, [invalidate]);

  // A different owner never sees the previous owner's value or late response.
  useEffect(() => {
    invalidate();
    setShown(null);
    setBusy(false);
  }, [owner, invalidate]);

  const run = useCallback(
    async (call: (signal: AbortSignal) => Promise<T>): Promise<OneTimeOutcome<T>> => {
      invalidate();
      const mine: Pending = { controller: new AbortController(), key: getApiKey(), owner };
      current.current = mine;
      setShown(null);
      setBusy(true);
      const stale = () =>
        current.current !== mine || mine.controller.signal.aborted || getApiKey() !== mine.key;
      try {
        const value = await call(mine.controller.signal);
        if (stale()) return { kind: "stale" };
        setShown({ owner: mine.owner, value });
        return { kind: "ok", value };
      } catch (error) {
        if (stale()) return { kind: "stale" };
        return { kind: "error", error };
      } finally {
        if (current.current === mine) {
          current.current = null;
          setBusy(false);
        }
      }
    },
    [owner, invalidate],
  );

  const clear = useCallback(() => setShown(null), []);

  return { value: shown && shown.owner === owner ? shown.value : null, busy, run, clear };
}
