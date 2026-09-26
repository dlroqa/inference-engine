import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type ModelInfo } from "../lib/api";
import type { AsyncStatus } from "./useAsync";
import { useAuthFailure } from "./useAuthScope";

export interface ModelsState {
  status: AsyncStatus;
  models: ModelInfo[];
  error: string | null;
  reload: () => void;
}

// Lists registry models and refreshes on an interval so download progress bars
// and load/active state stay current without manual refresh.
export function useModels(pollMs = 1500): ModelsState {
  const [status, setStatus] = useState<AsyncStatus>("loading");
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const reportAuthFailure = useAuthFailure();
  const authRef = useRef(reportAuthFailure);
  authRef.current = reportAuthFailure;

  const load = useCallback(async () => {
    try {
      const { models: rows } = await api.listModels();
      if (!alive.current) return;
      setModels(rows);
      setError(null);
      setStatus("ready");
    } catch (e) {
      if (!alive.current || authRef.current(e)) return;
      setError((e as Error).message);
      setStatus((s) => (s === "ready" ? s : "error"));
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void load();
    const id = setInterval(load, pollMs);
    return () => {
      alive.current = false;
      clearInterval(id);
    };
  }, [load, pollMs]);

  return { status, models, error, reload: load };
}

export type ModelDetailState =
  | { kind: "idle" }
  | { kind: "loading" }
  /** `stale` is set when a background refresh failed; `model` is the last good copy. */
  | { kind: "ready"; model: ModelInfo; stale: boolean; error: string | null }
  | { kind: "not-found" }
  | { kind: "error"; error: string };

export interface ModelDetail {
  state: ModelDetailState;
  /** Fetches again now (Retry); supersedes any request in flight. */
  retry: () => void;
}

// Every field the details drawer shows that can change after the model is
// added. When the polled list reports a different value, the drawer refetches.
function fingerprint(m: ModelInfo): string {
  return JSON.stringify([
    m.name, m.status, m.loaded, m.active, m.progress, m.downloaded_bytes, m.size_bytes,
    m.error, m.sha256, m.compat.status, m.compat.reason, m.quant, m.arch,
    m.context_length, m.source_type, m.source_ref,
  ]);
}

function isNotFound(e: unknown): boolean {
  return e instanceof ApiError && e.status === 404;
}

// One model's details (GET /admin/models/{id}) for the drawer, kept consistent
// with the list's polling rather than polling on its own:
// - a changed list row (or its confirmed disappearance from a successful list
//   refresh) triggers a background refresh; a list that is loading or failing
//   is not evidence of anything and triggers nothing;
// - every request is ordered: a superseded response, one for an earlier id, or
//   one arriving after close or unmount is ignored;
// - background refreshes are coalesced (one in flight, at most one queued), so
//   a fast-changing row cannot starve the drawer of responses;
// - a failed background refresh keeps the last details, marked stale with the
//   error; a confirmed 404 replaces them; auth loss ends the session.
export function useModelDetail(id: string | null, list: ModelsState): ModelDetail {
  const [state, setState] = useState<ModelDetailState>({ kind: "idle" });
  const seq = useRef(0);
  const inFlight = useRef(false);
  const queued = useRef(false);
  const idRef = useRef(id);
  idRef.current = id;
  const seen = useRef<{ id: string; fp: string } | null>(null);
  const reportAuthFailure = useAuthFailure();
  const authRef = useRef(reportAuthFailure);
  authRef.current = reportAuthFailure;

  const fetchNow: (target: string) => void = useCallback((target: string) => {
    const mine = ++seq.current;
    inFlight.current = true;
    queued.current = false;
    const current = () => mine === seq.current && idRef.current === target;
    api.getModel(target).then(
      (model) => {
        if (!current()) return;
        inFlight.current = false;
        setState({ kind: "ready", model, stale: false, error: null });
        if (queued.current) fetchNow(target);
      },
      (e: unknown) => {
        if (!current()) return;
        inFlight.current = false;
        if (authRef.current(e)) return;
        if (isNotFound(e)) {
          setState({ kind: "not-found" });
        } else {
          const error = (e as Error).message;
          setState((s) =>
            s.kind === "ready" ? { ...s, stale: true, error } : { kind: "error", error },
          );
        }
        if (queued.current) fetchNow(target);
      },
    );
  }, []);

  // Open, id change and close. A new id supersedes everything for the old one.
  useEffect(() => {
    seen.current = null;
    if (id === null) {
      seq.current += 1;
      inFlight.current = false;
      queued.current = false;
      setState({ kind: "idle" });
      return;
    }
    setState({ kind: "loading" });
    fetchNow(id);
  }, [id, fetchNow]);

  // Invalidate on unmount so a late response is dropped.
  useEffect(
    () => () => {
      seq.current += 1;
    },
    [],
  );

  // What the list confirms about this model: its row's fingerprint, "absent"
  // after a successful refresh without it, or null when the list proves nothing.
  const row = id === null ? undefined : list.models.find((m) => m.id === id);
  const confirmed =
    id === null || list.status !== "ready" || list.error !== null ? null : row ? fingerprint(row) : "absent";

  useEffect(() => {
    if (id === null || confirmed === null) return;
    const prev = seen.current;
    seen.current = { id, fp: confirmed };
    if (!prev || prev.id !== id || prev.fp === confirmed) return;
    if (inFlight.current) queued.current = true;
    else fetchNow(id);
  }, [id, confirmed, fetchNow]);

  const retry = useCallback(() => {
    const target = idRef.current;
    if (target === null) return;
    setState((s) => (s.kind === "ready" ? s : { kind: "loading" }));
    fetchNow(target);
  }, [fetchNow]);

  return { state, retry };
}
