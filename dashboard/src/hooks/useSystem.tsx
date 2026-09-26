import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from "react";
import { api, ApiError, type SwitchName, type SystemInfo } from "../lib/api";
import { useAuthFailure } from "./useAuthScope";

// One shared copy of GET /admin/system for the signed-in dashboard, used to
// explain which actions a feature switch disables. It is fetched once per
// operator session (the provider is remounted when the key changes, so a late
// response from an earlier session is discarded), refreshed on demand, on
// reconnection, and after the engine refuses an action because of a switch.
//
// Unknown state (loading, or a failed fetch) never counts as "enabled" or
// "disabled": actions stay available and the engine enforces the switch.
// A 401 or operator_role_required is not a failed fetch: it ends the session
// (the shell remounts this provider), so no earlier data is kept as "stale".

export type SystemStatus = "absent" | "loading" | "ready" | "error";

export interface SystemView {
  /** "absent" when no provider is mounted (the switch state is simply unknown). */
  status: SystemStatus;
  info: SystemInfo | null;
  /** True when `info` is from an earlier fetch and the latest refresh failed. */
  stale: boolean;
  error: string | null;
  refresh: () => void;
  /** Records that the engine refused an action because `name` is off. */
  reportDenied: (name: SwitchName) => void;
  /** true/false when known, null when unknown. */
  switchState: (name: SwitchName) => boolean | null;
  /** The names in `names` that are known to be off, from the latest state. */
  offSwitches: (names: readonly SwitchName[] | undefined) => SwitchName[];
}

interface State {
  status: Exclude<SystemStatus, "absent">;
  info: SystemInfo | null;
  stale: boolean;
  error: string | null;
  /** Switches the engine has refused since the last successful fetch. */
  denied: ReadonlySet<SwitchName>;
}

function stateOf(s: { info: SystemInfo | null; denied: ReadonlySet<SwitchName> }, name: SwitchName): boolean | null {
  if (s.denied.has(name)) return false;
  return s.info ? s.info.switches[name] : null;
}

const ABSENT: SystemView = {
  status: "absent",
  info: null,
  stale: false,
  error: null,
  refresh: () => {},
  reportDenied: () => {},
  switchState: () => null,
  offSwitches: () => [],
};

const SystemContext = createContext<SystemView>(ABSENT);

export function useSystem(): SystemView {
  return useContext(SystemContext);
}

function message(e: unknown): string {
  if (e instanceof ApiError) return e.status === 0 ? "the engine could not be reached" : `${e.message} (HTTP ${e.status})`;
  return String(e);
}

export function SystemProvider({ children }: { children: ReactNode }): JSX.Element {
  const [state, setState] = useState<State>({
    status: "loading",
    info: null,
    stale: false,
    error: null,
    denied: new Set(),
  });
  // Every fetch gets a generation; only the newest one may update state.
  const generation = useRef(0);
  const alive = useRef(false);
  // The latest state, for async handlers that re-check after an await.
  const latest = useRef(state);
  latest.current = state;
  const reportAuthFailure = useAuthFailure();
  const authRef = useRef(reportAuthFailure);
  authRef.current = reportAuthFailure;

  const refresh = useCallback(() => {
    const mine = ++generation.current;
    setState((p) => (p.info ? p : { ...p, status: "loading", error: null }));
    api.system().then(
      (info) => {
        if (!alive.current || mine !== generation.current) return;
        setState({ status: "ready", info, stale: false, error: null, denied: new Set() });
      },
      (e: unknown) => {
        if (!alive.current || mine !== generation.current) return;
        if (authRef.current(e)) return;
        setState((p) => ({
          ...p,
          status: p.info ? "ready" : "error",
          stale: p.info !== null,
          error: message(e),
        }));
      },
    );
  }, []);

  const reportDenied = useCallback(
    (name: SwitchName) => {
      setState((p) => (p.denied.has(name) ? p : { ...p, denied: new Set([...p.denied, name]) }));
      refresh();
    },
    [refresh],
  );

  useEffect(() => {
    alive.current = true;
    refresh();
    const onOnline = () => refresh();
    const onVisible = () => {
      const s = latest.current;
      if (document.visibilityState === "visible" && (s.status === "error" || s.stale)) refresh();
    };
    window.addEventListener("online", onOnline);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive.current = false;
      generation.current++;
      window.removeEventListener("online", onOnline);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh]);

  const view = useMemo<SystemView>(
    () => ({
      status: state.status,
      info: state.info,
      stale: state.stale,
      error: state.error,
      refresh,
      reportDenied,
      switchState: (name) => stateOf(state, name),
      offSwitches: (names) => (names ?? []).filter((n) => stateOf(latest.current, n) === false),
    }),
    [state, refresh, reportDenied],
  );

  return <SystemContext.Provider value={view}>{children}</SystemContext.Provider>;
}
