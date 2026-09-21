import { useState, type JSX } from "react";
import { api, ApiError, type ModelInfo, type DownloadRequest } from "../lib/api";
import { useModels } from "../hooks/useModels";
import { AsyncBoundary } from "../components/Panel";
import { Badge, type ToneName } from "../components/widgets";
import { Icon } from "../components/Icon";
import { bytes } from "../lib/format";

type AddMode = "huggingface" | "url" | "import";

const COMPAT_TONE: Record<string, ToneName> = {
  ok: "ok",
  too_large: "warn",
  needs_backend: "warn",
  unknown: "neutral",
};

const STATUS_TONE: Record<string, ToneName> = {
  ready: "ok",
  downloading: "neutral",
  verifying: "neutral",
  error: "danger",
  cancelled: "neutral",
};

function bytesProgress(m: ModelInfo): string {
  if (m.size_bytes) return `${bytes(m.downloaded_bytes)} / ${bytes(m.size_bytes)}`;
  return bytes(m.downloaded_bytes);
}

function AddModel({ onAdded }: { onAdded: () => void }): JSX.Element {
  const [mode, setMode] = useState<AddMode>("huggingface");
  const [repo, setRepo] = useState("");
  const [filename, setFilename] = useState("");
  const [url, setUrl] = useState("");
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [sha, setSha] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "import") {
        await api.importModel(path.trim(), name.trim() || null);
      } else {
        const body: DownloadRequest = {
          source_type: mode,
          name: name.trim() || null,
          expected_sha256: sha.trim() || null,
        };
        if (mode === "huggingface") {
          body.repo = repo.trim();
          body.filename = filename.trim();
        } else {
          body.url = url.trim();
        }
        await api.downloadModel(body);
      }
      setRepo("");
      setFilename("");
      setUrl("");
      setPath("");
      setName("");
      setSha("");
      onAdded();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const tabs: { id: AddMode; label: string }[] = [
    { id: "huggingface", label: "Hugging Face" },
    { id: "url", label: "URL" },
    { id: "import", label: "Local file" },
  ];

  return (
    <div className="card col-12">
      <h2>Add a model</h2>
      <div className="row" role="tablist" aria-label="Add model source" style={{ marginBottom: 16 }}>
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={mode === t.id}
            className={`btn ${mode === t.id ? "primary" : ""}`}
            onClick={() => setMode(t.id)}
            type="button"
          >
            {t.label}
          </button>
        ))}
      </div>

      <form onSubmit={submit}>
        <div className="grid">
          {mode === "huggingface" && (
            <>
              <div className="field col-6">
                <label htmlFor="repo">Repository</label>
                <input
                  id="repo"
                  className="input"
                  placeholder="bartowski/SmolLM2-135M-Instruct-GGUF"
                  value={repo}
                  onChange={(e) => setRepo(e.target.value)}
                  required
                />
              </div>
              <div className="field col-6">
                <label htmlFor="filename">Filename</label>
                <input
                  id="filename"
                  className="input"
                  placeholder="SmolLM2-135M-Instruct-Q4_K_M.gguf"
                  value={filename}
                  onChange={(e) => setFilename(e.target.value)}
                  required
                />
              </div>
            </>
          )}
          {mode === "url" && (
            <div className="field col-12">
              <label htmlFor="url">GGUF URL</label>
              <input
                id="url"
                className="input"
                placeholder="https://…/model-Q4_K_M.gguf"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                required
              />
            </div>
          )}
          {mode === "import" && (
            <div className="field col-12">
              <label htmlFor="path">Local file path</label>
              <input
                id="path"
                className="input mono"
                placeholder="/path/to/model.gguf"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                required
              />
            </div>
          )}

          <div className="field col-4">
            <label htmlFor="name">Name (optional)</label>
            <input
              id="name"
              className="input"
              placeholder="served model id"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          {mode !== "import" && (
            <div className="field col-8">
              <label htmlFor="sha">Expected SHA-256 (optional, verifies the download)</label>
              <input
                id="sha"
                className="input mono"
                placeholder="checksum"
                value={sha}
                onChange={(e) => setSha(e.target.value)}
              />
            </div>
          )}
        </div>

        {error && (
          <div className="banner err" role="alert">
            {error}
          </div>
        )}
        <button className="btn primary" type="submit" disabled={busy}>
          {busy ? <span className="spinner" /> : <Icon name="play" size={16} />}
          {mode === "import" ? "Import model" : "Download model"}
        </button>
      </form>
    </div>
  );
}

function ModelRow({
  model,
  onChange,
}: {
  model: ModelInfo;
  onChange: () => void;
}): JSX.Element {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChange();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const meta = [model.arch, model.quant, bytes(model.size_bytes)].filter(Boolean).join(" · ");

  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div className="row spread">
        <div>
          <div className="row" style={{ gap: 8 }}>
            <span className="mono" style={{ fontWeight: 600 }}>
              {model.name}
            </span>
            {model.loaded && <Badge tone="ok">loaded</Badge>}
            <Badge tone={STATUS_TONE[model.status] ?? "neutral"}>{model.status}</Badge>
            <Badge tone={COMPAT_TONE[model.compat.status] ?? "neutral"}>{model.compat.status}</Badge>
          </div>
          <div className="sub">
            {meta || "—"} · <span className="muted">{model.source_type}</span>
          </div>
          {model.compat.status !== "ok" && (
            <div className="sub" title={model.compat.reason}>
              {model.compat.reason}
            </div>
          )}
        </div>
        <div className="row">
          {model.status === "downloading" && (
            <button className="btn" onClick={() => run(() => api.cancelDownload(model.id))} disabled={busy}>
              <Icon name="stop" size={16} /> Cancel
            </button>
          )}
          {model.status === "ready" && !model.loaded && (
            <button
              className="btn primary"
              onClick={() => run(() => api.loadModelById(model.id))}
              disabled={busy}
            >
              {busy ? <span className="spinner" /> : <Icon name="play" size={16} />} Load
            </button>
          )}
          {model.loaded && (
            <button className="btn" onClick={() => run(() => api.unloadModelById(model.id))} disabled={busy}>
              <Icon name="stop" size={16} /> Unload
            </button>
          )}
          {!model.loaded && model.status !== "downloading" && (
            <button
              className="btn danger"
              onClick={() => run(() => api.deleteModel(model.id))}
              disabled={busy}
              aria-label={`Delete ${model.name}`}
            >
              <Icon name="trash" size={16} /> Delete
            </button>
          )}
        </div>
      </div>

      {model.status === "downloading" && (
        <div className="meter" style={{ marginTop: 12 }}>
          <div className="head">
            <span>Downloading</span>
            <span className="tabular muted">{bytesProgress(model)}</span>
          </div>
          <div
            className="track"
            role="meter"
            aria-label="Download progress"
            aria-valuenow={model.progress != null ? Math.round(model.progress * 100) : undefined}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div className="fill" style={{ width: `${(model.progress ?? 0) * 100}%` }} />
          </div>
        </div>
      )}
      {model.status === "error" && model.error && (
        <div className="banner err" role="alert" style={{ marginTop: 12 }}>
          {model.error}
        </div>
      )}
      {error && (
        <div className="banner err" role="alert" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}
    </div>
  );
}

export function Models(): JSX.Element {
  const { status, models, error, reload } = useModels();

  return (
    <>
      <div className="topbar">
        <h1>Models</h1>
        <button className="btn" onClick={reload}>
          <Icon name="refresh" size={16} /> Refresh
        </button>
      </div>

      <div className="grid" style={{ marginBottom: 20 }}>
        <AddModel onAdded={reload} />
      </div>

      <AsyncBoundary
        status={status}
        error={error}
        isEmpty={models.length === 0}
        emptyText="No models yet. Download one from Hugging Face or a URL, or import a local GGUF file."
        onRetry={reload}
      >
        {models.map((m) => (
          <ModelRow key={m.id} model={m} onChange={reload} />
        ))}
      </AsyncBoundary>
    </>
  );
}
