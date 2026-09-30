import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type Alert, type BackendsPage, type ModelInfo, type SystemInfo } from "../lib/api";
import type { Observations, Source } from "../lib/architecture";
import { useAuthFailure } from "./useAuthScope";
import { useLiveMetrics } from "./useLiveMetrics";
import { useSystem } from "./useSystem";

// Everything the Architecture view observes, with one owner for its requests.
//
// - /admin/system comes from the session's shared SystemProvider, and live
//   metrics from useLiveMetrics (stream, polling fallback, its own ownership
//   rules). Neither is fetched a second time here.
// - /admin/backends, /admin/models and /admin/alerts are read when the view
//   opens and on Refresh. Each source is independent: one failing leaves the
//   others usable. Every read carries a per-source generation; only the newest
//   read of a mounted view may change state, so a late success or failure
//   (after Refresh, leaving the view, or a key change, which remounts the
//   session tree) is dropped before it can touch state or report auth loss.
// - A failed refresh keeps the previous data, marked stale, with the time it
//   was observed. An auth failure ends the session through the shell instead.

type Fetched = {
  backends: BackendsPage;
  models: { models: ModelInfo[] };
  alerts: { alerts: Alert[] };
};
type Key = keyof Fetched;

const KEYS: Key[] = ["backends", "models", "alerts"];

// Looked up at call time, so tests can replace api methods.
const FETCH: { [K in Key]: () => Promise<Fetched[K]> } = {
  backends: () => api.backends(),
  models: () => api.listModels(),
  alerts: () => api.alerts(),
};

function loading<T>(): Source<T> {
  return { status: "loading", data: null, error: null, observedAt: null, stale: false };
}

function describe(e: unknown): string {
  if (e instanceof ApiError) return e.status === 0 ? "the engine could not be reached" : `${e.message} (HTTP ${e.status})`;
  return String(e);
}

export interface ArchitectureData {
  observations: Observations;
  /** Re-reads every fetched source and the system summary. */
  refresh: () => void;
}

export function useArchitectureData(): ArchitectureData {
  const system = useSystem();
  const metrics = useLiveMetrics();
  const reportAuthFailure = useAuthFailure();
  const authRef = useRef(reportAuthFailure);
  authRef.current = reportAuthFailure;

  const [sources, setSources] = useState<{ [K in Key]: Source<Fetched[K]> }>(() => ({
    backends: loading(),
    models: loading(),
    alerts: loading(),
  }));
  const generation = useRef<Record<Key, number>>({ backends: 0, models: 0, alerts: 0 });
  const alive = useRef(false);

  const load = useCallback(<K extends Key>(key: K) => {
    const mine = ++generation.current[key];
    const current = () => alive.current && mine === generation.current[key];
    setSources((p) => (p[key].data ? p : ({ ...p, [key]: { ...p[key], status: "loading", error: null } } as typeof p)));
    FETCH[key]().then(
      (data) => {
        if (!current()) return;
        setSources(
          (p) =>
            ({
              ...p,
              [key]: { status: "ready", data, error: null, observedAt: Date.now() / 1000, stale: false },
            }) as typeof p,
        );
      },
      (e: unknown) => {
        if (!current()) return;
        if (authRef.current(e)) return;
        setSources(
          (p) =>
            ({
              ...p,
              [key]: { ...p[key], status: "error", error: describe(e), stale: p[key].data !== null },
            }) as typeof p,
        );
      },
    );
  }, []);

  useEffect(() => {
    alive.current = true;
    KEYS.forEach(load);
    const gens = generation.current;
    return () => {
      alive.current = false;
      KEYS.forEach((k) => gens[k]++);
    };
  }, [load]);

  const refreshSystem = system.refresh;
  const refresh = useCallback(() => {
    refreshSystem();
    KEYS.forEach(load);
  }, [load, refreshSystem]);

  const systemSource = useMemo<Source<SystemInfo>>(() => {
    if (system.status === "absent") {
      return { status: "error", data: null, error: "no system summary in this context", observedAt: null, stale: false };
    }
    return {
      status: system.status === "ready" && !system.stale ? "ready" : system.status === "loading" ? "loading" : "error",
      data: system.info,
      error: system.error,
      observedAt: system.observedAt,
      stale: system.stale,
    };
  }, [system.status, system.info, system.error, system.observedAt, system.stale]);

  const observations = useMemo<Observations>(
    () => ({
      system: systemSource,
      metrics: { snapshot: metrics.snapshot, status: metrics.status, fresh: metrics.fresh },
      backends: sources.backends,
      models: sources.models,
      alerts: sources.alerts,
    }),
    [systemSource, metrics.snapshot, metrics.status, metrics.fresh, sources],
  );

  return { observations, refresh };
}
