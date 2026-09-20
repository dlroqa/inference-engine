import { useState, type JSX } from "react";
import { api, ApiError, type ModelPanel } from "../lib/api";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/Panel";
import { Badge, stateTone } from "../components/widgets";
import { Icon } from "../components/Icon";

export function Models(): JSX.Element {
  const { status, data, error, reload } = useAsync<ModelPanel>(
    () => api.overview().then((o) => o.model),
    [],
  );
  const [busy, setBusy] = useState<null | "load" | "unload">(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const act = async (which: "load" | "unload") => {
    setBusy(which);
    setActionError(null);
    try {
      if (which === "load") await api.loadModel();
      else await api.unloadModel();
      reload();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <div className="topbar">
        <h1>Models</h1>
      </div>

      <div className="card col-12" style={{ maxWidth: 640 }}>
        <h2>Configured model</h2>
        <AsyncBoundary status={status} error={error} onRetry={reload}>
          {data && (
            <>
              <div className="row spread" style={{ marginBottom: 16 }}>
                <div>
                  <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>
                    {data.configured_id}
                  </div>
                  <div className="sub">
                    {data.configured ? "Model path configured" : "No model path configured"}
                  </div>
                </div>
                <Badge tone={stateTone(data.state)}>{data.state}</Badge>
              </div>

              {actionError && (
                <div className="banner err" role="alert">
                  {actionError}
                </div>
              )}

              <div className="row">
                <button
                  className="btn primary"
                  onClick={() => act("load")}
                  disabled={busy !== null || data.loaded || !data.configured}
                >
                  {busy === "load" ? <span className="spinner" /> : <Icon name="play" size={16} />}
                  Load model
                </button>
                <button
                  className="btn"
                  onClick={() => act("unload")}
                  disabled={busy !== null || !data.loaded}
                >
                  {busy === "unload" ? <span className="spinner" /> : <Icon name="stop" size={16} />}
                  Unload
                </button>
                <button className="btn" onClick={reload} disabled={busy !== null}>
                  <Icon name="refresh" size={16} /> Refresh
                </button>
              </div>

              {!data.configured && (
                <div className="sub" style={{ marginTop: 12 }}>
                  Set <span className="mono">model_path</span> in the engine config to enable loading.
                </div>
              )}
            </>
          )}
        </AsyncBoundary>
      </div>
    </>
  );
}
