import { useState, type JSX, type ReactNode } from "react";
import type { ModelInfo } from "../lib/api";
import type { ModelDetailState } from "../hooks/useModels";
import { Dialog } from "../components/Dialog";
import { Badge } from "../components/widgets";
import { Icon } from "../components/Icon";
import { WiredTo } from "../components/WiredTo";
import { bytes, num } from "../lib/format";

// Presentation rule for a model's source (docs/dashboard.md, "Model details"):
// the engine returns `source_ref` with credentials redacted, but a local
// import's `source_ref` is its filesystem path, so the drawer never renders it
// and shows a neutral label instead. Remote references are shown as plain text,
// never as links. Error text is shown as text; the engine redacts credential-
// bearing URLs in it, but it may still name a path the engine reported.
export function sourceText(m: ModelInfo): string {
  if (m.source_type === "import") return "Local import (path not shown)";
  const kind = m.source_type === "huggingface" ? "Hugging Face" : "URL";
  return m.source_ref ? `${kind}: ${m.source_ref}` : kind;
}

function Row({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function CopyChecksum({ sha }: { sha: string }): JSX.Element {
  const [result, setResult] = useState<"copied" | "failed" | null>(null);
  const copy = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(sha);
      setResult("copied");
    } catch {
      setResult("failed");
    }
  };
  return (
    <div className="row" data-wiring="local.copy-checksum" style={{ marginTop: 8 }}>
      <button type="button" className="btn" onClick={() => void copy()}>
        <Icon name="copy" size={16} /> Copy checksum
      </button>
      <WiredTo id="local.copy-checksum" />
      <span role="status" className="sub">
        {result === "copied" && "Checksum copied to the clipboard."}
        {result === "failed" && "Could not copy: clipboard access was refused. Select the checksum text to copy it."}
      </span>
    </div>
  );
}

function Details({ m }: { m: ModelInfo }): JSX.Element {
  return (
    <dl className="kv kv-rows" data-testid="model-details">
      <Row label="Name">
        <span className="mono wrap-anywhere">{m.name}</span>
      </Row>
      <Row label="ID">
        <span className="mono wrap-anywhere">{m.id}</span>
      </Row>
      <Row label="Status">
        <span className="row" style={{ gap: 8 }}>
          <Badge tone={m.status === "ready" ? "ok" : m.status === "error" ? "danger" : "neutral"}>{m.status}</Badge>
          {m.loaded && <Badge tone="ok">loaded</Badge>}
          {m.status === "downloading" && m.progress !== null && (
            <span className="tabular muted">{Math.round(m.progress * 100)}%</span>
          )}
        </span>
      </Row>
      <Row label="Compatibility">
        <Badge tone={m.compat.status === "ok" ? "ok" : m.compat.status === "unknown" ? "neutral" : "warn"}>
          {m.compat.status}
        </Badge>{" "}
        <span className="muted">{m.compat.reason}</span>
      </Row>
      <Row label="Architecture">{m.arch ?? "—"}</Row>
      <Row label="Quantization">{m.quant ?? "—"}</Row>
      <Row label="Context length">
        <span className="tabular">{m.context_length !== null ? num(m.context_length) : "—"}</span>
      </Row>
      <Row label="Size">
        <span className="tabular">{bytes(m.size_bytes)}</span>
      </Row>
      <Row label="Added">
        <span className="tabular">{m.added_at}</span>
      </Row>
      <Row label="Source">
        <span className="wrap-anywhere" data-testid="model-source">
          {sourceText(m)}
        </span>
      </Row>
      <Row label="SHA-256">
        {m.sha256 ? (
          <>
            <span className="mono wrap-anywhere" data-testid="model-sha256">
              {m.sha256}
            </span>
            <CopyChecksum sha={m.sha256} />
          </>
        ) : (
          <span className="muted">not recorded</span>
        )}
      </Row>
      {m.error && (
        <Row label="Error">
          <span className="wrap-anywhere">{m.error}</span>
        </Row>
      )}
    </dl>
  );
}

export function ModelDrawer({
  state,
  onClose,
  onRetry,
  returnFocus,
}: {
  state: ModelDetailState;
  onClose: () => void;
  onRetry: () => void;
  returnFocus: () => HTMLElement | null;
}): JSX.Element {
  const title = state.kind === "ready" ? `Model details: ${state.model.name}` : "Model details";
  let body: JSX.Element;
  if (state.kind === "ready") {
    body = (
      <>
        {state.stale && (
          <div className="banner err" role="alert" data-wiring="models.detail">
            Could not refresh these details ({state.error}). Showing the last details received.{" "}
            <button className="btn" onClick={onRetry} style={{ marginLeft: 8 }}>
              Retry
            </button>
          </div>
        )}
        <Details m={state.model} />
      </>
    );
  } else if (state.kind === "not-found") {
    body = (
      <div className="banner err" role="alert">
        This model no longer exists in the registry (it may have been deleted).
      </div>
    );
  } else if (state.kind === "error") {
    body = (
      <div className="banner err" role="alert" data-wiring="models.detail">
        {state.error}{" "}
        <button className="btn" onClick={onRetry} style={{ marginLeft: 8 }}>
          Retry
        </button>
      </div>
    );
  } else {
    body = (
      <div className="empty row" style={{ justifyContent: "center" }}>
        <span className="spinner" role="status" aria-label="Loading" /> Loading…
      </div>
    );
  }
  return (
    <Dialog title={title} onClose={onClose} variant="drawer" returnFocus={returnFocus}>
      <div className="row drawer-actions" data-wiring="local.close-drawer">
        <button type="button" className="btn" onClick={onClose}>
          <Icon name="x" size={16} /> Close
        </button>
        <WiredTo id={["models.detail", "local.close-drawer"]} label="Model details" />
      </div>
      {body}
    </Dialog>
  );
}
