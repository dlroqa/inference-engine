import { useState, type JSX } from "react";
import { api, ApiError, type AuditPage } from "../lib/api";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/Panel";
import { Badge } from "../components/widgets";
import { Icon } from "../components/Icon";
import { WiredTo } from "../components/WiredTo";
import { clockTime } from "../lib/format";
import { useAuthFailure } from "../hooks/useAuthScope";

export function Security(): JSX.Element {
  const [verify, setVerify] = useState<AuditPage["verify"] | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [verifyError, setVerifyError] = useState<string | null>(null);
  const { status, data, error, reload } = useAsync(() => api.audit({ limit: 200 }), []);
  const reportAuthFailure = useAuthFailure();

  const runVerify = async () => {
    setVerifying(true);
    setVerifyError(null);
    try {
      const res = await api.audit({ limit: 1000, verify: true });
      setVerify(res.verify ?? null);
    } catch (err) {
      if (reportAuthFailure(err)) return;
      // A failed request is not a verification result: say so instead of
      // silently showing nothing.
      setVerify(null);
      setVerifyError(
        `Integrity check could not run: ${err instanceof ApiError ? err.message : String(err)}`,
      );
    } finally {
      setVerifying(false);
    }
  };

  const events = data?.events ?? [];

  return (
    <>
      <div className="topbar">
        <h1>Security &amp; audit</h1>
        <div className="row">
          <button className="btn" onClick={runVerify} disabled={verifying} data-wiring="security.audit">
            {verifying ? <span className="spinner" /> : <Icon name="shield" size={16} />}
            Verify integrity
          </button>
          <WiredTo id="security.audit" label="Verify integrity" />
          <button className="btn" onClick={reload} data-wiring="security.audit">
            <Icon name="refresh" size={16} /> Refresh
          </button>
          <WiredTo id="security.audit" label="Refresh" />
        </div>
      </div>

      {verifyError && (
        <div className="banner err" role="alert">
          {verifyError}
        </div>
      )}

      {verify && (
        <div className={`banner ${verify.ok ? "info" : "err"}`} role="status">
          {verify.ok ? (
            <>
              <Icon name="check" size={16} /> Hash chain verified — {verify.count} records intact.
            </>
          ) : (
            <>
              <Icon name="alert" size={16} /> Tamper detected — first bad record id {verify.first_bad_id}.
            </>
          )}
        </div>
      )}

      <div className="card col-12" data-wiring="security.audit">
        <div className="row panel-title">
          <h2>Operator audit log</h2>
          <WiredTo id="security.audit" label="Operator audit log" />
        </div>
        <p className="sub" style={{ marginTop: 0, marginBottom: 12 }}>
          Tamper-evident record of operator and security actions. Structured metadata only —
          never prompts, responses, or secrets.
        </p>
        <AsyncBoundary
          status={status}
          error={error}
          isEmpty={events.length === 0}
          emptyText="No audit records yet."
          onRetry={reload}
        >
          <div className="scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Actor</th>
                  <th>Action</th>
                  <th>Target</th>
                  <th>Detail</th>
                  <th>Hash</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id}>
                    <td className="tabular muted">{clockTime(e.ts)}</td>
                    <td className="mono">{e.actor ?? "—"}</td>
                    <td>
                      <Badge tone="neutral">{e.action}</Badge>
                    </td>
                    <td className="mono muted">{e.target ?? "—"}</td>
                    <td className="muted">
                      {e.detail ? (
                        <span className="sub">{JSON.stringify(e.detail)}</span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="mono muted">{e.hash.slice(0, 12)}…</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </AsyncBoundary>
      </div>
    </>
  );
}
