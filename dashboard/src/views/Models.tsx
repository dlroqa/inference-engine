import { useState, type JSX } from "react";
import { api, type ModelInfo, type DownloadRequest, type SwitchName } from "../lib/api";
import { useModels } from "../hooks/useModels";
import { AsyncBoundary } from "../components/Panel";
import { Badge, type ToneName } from "../components/widgets";
import { Icon } from "../components/Icon";
import { WiredTo } from "../components/WiredTo";
import { bytes } from "../lib/format";
import { useConfirm } from "../hooks/useConfirm";
import { useSystem } from "../hooks/useSystem";
import { useAuthFailure } from "../hooks/useAuthScope";
import { SystemNotice } from "../components/SystemNotice";
import { describeActionError, featureSwitchFor, switchReason } from "../lib/switches";
import { wiringFor } from "../lib/wiring";

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

// Switch-gated row actions share one explanation (SystemNotice) that each
// disabled button references. Cancelling a download is not switch-gated.
const ROW_NOTICE_ID = "models-switch-notice";
const ROW_ACTIONS = ["models.load", "models.unload", "models.delete"] as const;
const ROW_SWITCHES: SwitchName[] = [...new Set(ROW_ACTIONS.flatMap((id) => wiringFor(id).requiredSwitches ?? []))];

function switchesFor(id: string): SwitchName[] {
  return wiringFor(id).requiredSwitches ?? [];
}

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
  const sys = useSystem();
  const reportAuthFailure = useAuthFailure();

  const submitId = mode === "import" ? "models.import" : "models.download";
  // Every switch this mode needs that is known to be off.
  const off = sys.offSwitches(switchesFor(submitId));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    // Re-check at submission: the state may have changed since render.
    const nowOff = sys.offSwitches(switchesFor(submitId));
    if (nowOff.length > 0) {
      setError(switchReason(nowOff));
      return;
    }
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
      if (reportAuthFailure(err)) return;
      setError(describeActionError(err));
      const denied = featureSwitchFor(err);
      if (denied) sys.reportDenied(denied);
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
    <div className="card col-12" data-wiring={submitId}>
      <div className="row panel-title">
        <h2>Add a model</h2>
        <WiredTo id={["models.download", "models.import"]} label="Add a model" />
      </div>
      <div className="row" style={{ marginBottom: 16 }} data-wiring="local.add-source">
        <div className="row" role="tablist" aria-label="Add model source">
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
        <WiredTo id="local.add-source" />
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
        {off.length > 0 && (
          <p className="disabled-reason" id="add-model-restriction">
            <Icon name="lock" size={14} />
            <span>
              {switchReason(off)} {mode === "import" ? "Importing" : "Downloading"} models is unavailable until the
              engine's configuration turns {off.length === 1 ? "it" : "them"} on.
            </span>
          </p>
        )}
        <div className="row">
          <button
            className="btn primary"
            type="submit"
            disabled={busy || off.length > 0}
            aria-describedby={off.length > 0 ? "add-model-restriction" : undefined}
          >
            {busy ? <span className="spinner" /> : <Icon name="play" size={16} />}
            {mode === "import" ? "Import model" : "Download model"}
          </button>
          <WiredTo id={submitId} />
        </div>
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
  const [confirm, confirmDialog] = useConfirm();
  const sys = useSystem();
  const reportAuthFailure = useAuthFailure();

  // Switches (known to be off) that block a row action; none for cancel.
  const blockedBy = (id: string) => sys.offSwitches(switchesFor(id));
  const rowBlocked = (id: string) => blockedBy(id).length > 0;
  const describedBy = (id: string) => (rowBlocked(id) ? ROW_NOTICE_ID : undefined);

  const run = async (id: string, fn: () => Promise<unknown>) => {
    // Re-check when the action runs, including after a confirmation dialog.
    const off = blockedBy(id);
    if (off.length > 0) {
      setError(switchReason(off));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChange();
    } catch (err) {
      if (reportAuthFailure(err)) return;
      setError(describeActionError(err));
      const denied = featureSwitchFor(err);
      if (denied) sys.reportDenied(denied);
    } finally {
      setBusy(false);
    }
  };

  const meta = [model.arch, model.quant, bytes(model.size_bytes)].filter(Boolean).join(" · ");

  // The explanation lists exactly the actions this row currently offers.
  const actions = [
    model.status === "downloading" && "models.cancel",
    model.status === "ready" && !model.loaded && "models.load",
    model.loaded && "models.unload",
    !model.loaded && model.status !== "downloading" && "models.delete",
  ].filter((a): a is string => Boolean(a));

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
            <button
              className="btn"
              onClick={() => run("models.cancel", () => api.cancelDownload(model.id))}
              disabled={busy}
              data-wiring="models.cancel"
            >
              <Icon name="stop" size={16} /> Cancel
            </button>
          )}
          {model.status === "ready" && !model.loaded && (
            <button
              className="btn primary"
              onClick={() => run("models.load", () => api.loadModelById(model.id))}
              disabled={busy || rowBlocked("models.load")}
              aria-describedby={describedBy("models.load")}
              data-wiring="models.load"
            >
              {busy ? <span className="spinner" /> : <Icon name="play" size={16} />} Load
            </button>
          )}
          {model.loaded && (
            <button
              className="btn"
              onClick={() => run("models.unload", () => api.unloadModelById(model.id))}
              disabled={busy || rowBlocked("models.unload")}
              aria-describedby={describedBy("models.unload")}
              data-wiring="models.unload"
            >
              <Icon name="stop" size={16} /> Unload
            </button>
          )}
          {!model.loaded && model.status !== "downloading" && (
            <button
              className="btn danger"
              onClick={async () => {
                const ok = await confirm({
                  title: `Delete ${model.name}?`,
                  // Whether the file is engine-managed is not known here (the API does not
                  // expose paths), so both outcomes are stated.
                  body: "The model is removed from the registry. A file the engine manages (downloaded, or imported into the model store) is deleted and cannot be restored by this action. An imported file outside the model store stays on disk and can be imported again.",
                  confirmLabel: "Delete model",
                  wiring: "models.delete",
                });
                if (ok) await run("models.delete", () => api.deleteModel(model.id));
              }}
              disabled={busy || rowBlocked("models.delete")}
              aria-describedby={describedBy("models.delete")}
              aria-label={`Delete ${model.name}`}
              data-wiring="models.delete"
            >
              <Icon name="trash" size={16} /> Delete
            </button>
          )}
          {actions.length > 0 && <WiredTo id={actions} label={`Actions for ${model.name}`} />}
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
      {confirmDialog}
    </div>
  );
}

export function Models(): JSX.Element {
  const { status, models, error, reload } = useModels();
  const sys = useSystem();

  return (
    <>
      <div className="topbar">
        <div className="row">
          <h1>Models</h1>
          <WiredTo id="models.list" />
        </div>
        <div className="row">
          <button
            className="btn"
            onClick={() => {
              reload();
              sys.refresh();
            }}
            data-wiring="models.list app.system"
          >
            <Icon name="refresh" size={16} /> Refresh
          </button>
          <WiredTo id={["models.list", "app.system"]} label="Refresh" />
        </div>
      </div>

      <SystemNotice
        id={ROW_NOTICE_ID}
        switches={ROW_SWITCHES}
        consequence="Loading, unloading and deleting models is unavailable; listing models and cancelling downloads still work."
      />

      <div className="grid" style={{ marginBottom: 20 }}>
        <AddModel onAdded={reload} />
      </div>

      <div data-wiring="models.list">
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
      </div>
    </>
  );
}
