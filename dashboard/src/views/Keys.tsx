import { useState, type JSX } from "react";
import { api, ApiError, type CreatedKey, type KeyRow } from "../lib/api";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/Panel";
import { Badge } from "../components/widgets";
import { Icon } from "../components/Icon";

export function Keys(): JSX.Element {
  const { status, data, error, reload } = useAsync(() => api.listKeys(), []);
  const [label, setLabel] = useState("");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<CreatedKey | null>(null);
  const [copyOk, setCopyOk] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreating(true);
    setFormError(null);
    setCreated(null);
    try {
      const key = await api.createKey(label.trim() || null);
      setCreated(key);
      setLabel("");
      reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  const revoke = async (id: string) => {
    try {
      await api.revokeKey(id);
      reload();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : String(err));
    }
  };

  const copy = async () => {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.token);
      setCopyOk(true);
      setTimeout(() => setCopyOk(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };

  const keys: KeyRow[] = data?.keys ?? [];

  return (
    <>
      <div className="topbar">
        <h1>API keys</h1>
      </div>

      <div className="grid">
        <div className="card col-4" style={{ minWidth: 280 }}>
          <h2>Create key</h2>
          <form onSubmit={create}>
            <div className="field">
              <label htmlFor="label">Label (optional)</label>
              <input
                id="label"
                className="input"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="e.g. staging-app"
                maxLength={200}
              />
            </div>
            <button className="btn primary" type="submit" disabled={creating}>
              {creating ? <span className="spinner" /> : <Icon name="key" size={16} />}
              Create key
            </button>
          </form>

          {formError && (
            <div className="banner err" role="alert" style={{ marginTop: 12 }}>
              {formError}
            </div>
          )}

          {created && (
            <div className="banner info" role="status" style={{ marginTop: 12 }}>
              <strong>Save this token now — it is shown only once.</strong>
              <div className="token-box" style={{ marginTop: 8 }} data-testid="new-token">
                {created.token}
              </div>
              <button className="btn" onClick={copy} style={{ marginTop: 8 }}>
                <Icon name={copyOk ? "check" : "copy"} size={16} />
                {copyOk ? "Copied" : "Copy"}
              </button>
            </div>
          )}
        </div>

        <div className="card col-8">
          <div className="row spread" style={{ marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>Keys</h2>
            <button className="btn" onClick={reload}>
              <Icon name="refresh" size={16} /> Refresh
            </button>
          </div>
          <AsyncBoundary
            status={status}
            error={error}
            isEmpty={keys.length === 0}
            emptyText="No API keys yet."
            onRetry={reload}
          >
            <div className="scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>Prefix</th>
                    <th>Label</th>
                    <th>Created</th>
                    <th>Last used</th>
                    <th>Status</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {keys.map((k) => (
                    <tr key={k.id}>
                      <td className="mono">{k.prefix}…</td>
                      <td>{k.label ?? <span className="muted">—</span>}</td>
                      <td className="muted">{new Date(k.created_at).toLocaleDateString()}</td>
                      <td className="muted">
                        {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "never"}
                      </td>
                      <td>
                        {k.revoked ? (
                          <Badge tone="danger">revoked</Badge>
                        ) : (
                          <Badge tone="ok">active</Badge>
                        )}
                      </td>
                      <td style={{ textAlign: "right" }}>
                        {!k.revoked && (
                          <button
                            className="btn danger"
                            onClick={() => revoke(k.id)}
                            aria-label={`Revoke key ${k.prefix}`}
                          >
                            <Icon name="trash" size={16} /> Revoke
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </AsyncBoundary>
        </div>
      </div>
    </>
  );
}
