import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ModelInfo } from "../lib/api";
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
