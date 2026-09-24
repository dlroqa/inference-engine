import { useState, type JSX } from "react";
import { api, type FeedEvent } from "../lib/api";
import { useAsync } from "../hooks/useAsync";
import { useLiveMetrics } from "../hooks/useLiveMetrics";
import { useLiveFeed } from "../hooks/useLiveFeed";
import { AsyncBoundary, ConnBadge } from "../components/Panel";
import { Badge, alertTone, categoryTone, type ToneName } from "../components/widgets";
import { Icon } from "../components/Icon";
import { num, clockTime } from "../lib/format";

type NavFn = (view: string, opts?: { logQuery?: string; clientId?: string }) => void;
type Win = "5h" | "week";

const FILL: Record<ToneName, string> = { danger: "danger", warn: "warn", ok: "", neutral: "" };

export function Monitoring({ onNavigate }: { onNavigate: NavFn }): JSX.Element {
  const [win, setWin] = useState<Win>("5h");
  const alerts = useAsync(() => api.alerts(), []);
  const attribution = useAsync(() => api.usageAttribution(win), [win]);
  const taxonomy = useAsync(() => api.errorTaxonomy(win), [win]);
  const { status: metricsStatus } = useLiveMetrics();
  const { events, status: feedStatus } = useLiveFeed();

  const reloadAll = () => {
    alerts.reload();
    attribution.reload();
    taxonomy.reload();
  };

  const keys = attribution.data?.keys ?? [];
  const cats = taxonomy.data?.categories ?? {};
  const catMax = Math.max(1, ...Object.values(cats));
  const alertList = alerts.data?.alerts ?? [];

  return (
    <>
      <div className="topbar">
        <div className="row">
          <h1>Live monitoring</h1>
          <ConnBadge status={metricsStatus} />
        </div>
        <div className="row">
          <div className="segmented" role="group" aria-label="Time window">
            {(["5h", "week"] as Win[]).map((w) => (
              <button
                key={w}
                className="segbtn"
                aria-pressed={win === w}
                onClick={() => setWin(w)}
              >
                {w === "5h" ? "5 hours" : "This week"}
              </button>
            ))}
          </div>
          <button className="btn" onClick={reloadAll}>
            <Icon name="refresh" size={16} /> Refresh
          </button>
          <button className="btn" onClick={() => onNavigate("security")}>
            <Icon name="shield" size={16} /> Security
          </button>
        </div>
      </div>

      <div className="grid">
        {/* Alerts */}
        <div className="card col-12">
          <div className="row spread" style={{ marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>Alerts</h2>
            {alertList.length > 0 && <Badge tone="warn">{alertList.length}</Badge>}
          </div>
          <AsyncBoundary
            status={alerts.status}
            error={alerts.error}
            isEmpty={alertList.length === 0}
            emptyText="All clear — no active alerts."
            onRetry={alerts.reload}
          >
            <div>
              {alertList.map((a, i) => {
                const clickable = a.target_type === "client" && a.target_id;
                return (
                  <div className="feed-row" key={`${a.kind}-${a.target_id}-${i}`}>
                    <Badge tone={alertTone(a.severity)}>{a.severity}</Badge>
                    <span>{a.message}</span>
                    {clickable && (
                      <button
                        className="linkbtn"
                        onClick={() => onNavigate("clients", { clientId: a.target_id ?? undefined })}
                      >
                        View client <Icon name="external" size={13} />
                      </button>
                    )}
                    {a.target_type === "webhook_deliveries" && (
                      <button className="linkbtn" onClick={() => onNavigate("clients")}>
                        View clients <Icon name="external" size={13} />
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </AsyncBoundary>
        </div>

        {/* Error taxonomy */}
        <div className="card col-4">
          <h2>Error taxonomy</h2>
          <AsyncBoundary
            status={taxonomy.status}
            error={taxonomy.error}
            isEmpty={Object.keys(cats).length === 0}
            emptyText="No errors in this window."
            onRetry={taxonomy.reload}
          >
            <div>
              {Object.entries(cats).map(([cat, n]) => (
                <div className="meter" key={cat}>
                  <div className="head">
                    <span className="row" style={{ gap: 8 }}>
                      <Badge tone={categoryTone(cat)}>{cat}</Badge>
                    </span>
                    <span className="tabular muted">{num(n)}</span>
                  </div>
                  <div className="track">
                    <div
                      className={`fill ${FILL[categoryTone(cat)]}`}
                      style={{ width: `${(n / catMax) * 100}%` }}
                    />
                  </div>
                </div>
              ))}
              <div className="sub" style={{ marginTop: 12 }}>
                {num(taxonomy.data?.total ?? 0)} categorized errors
              </div>
            </div>
          </AsyncBoundary>
        </div>

        {/* Live feed with cross-link to logs */}
        <div className="card col-8">
          <div className="row spread" style={{ marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>Live feed</h2>
            <ConnBadge status={feedStatus} />
          </div>
          {feedStatus === "down" && (
            <div className="banner info">Feed disconnected — reconnecting…</div>
          )}
          <div className="scroll">
            {events.length === 0 ? (
              <div className="empty">No inference activity yet.</div>
            ) : (
              [...events].reverse().slice(0, 40).map((e: FeedEvent, i) => (
                <div className="feed-row" key={`${e.request_id}-${e.type}-${e.ts}-${i}`}>
                  <FeedBadge event={e} />
                  <span className="mono muted">{e.model ?? ""}</span>
                  {e.request_id && (
                    <button
                      className="linkbtn"
                      onClick={() => onNavigate("logs", { logQuery: e.request_id })}
                      aria-label={`View logs for request ${e.request_id}`}
                    >
                      logs <Icon name="external" size={13} />
                    </button>
                  )}
                  <time>{clockTime(e.ts)}</time>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Per-key attribution */}
        <div className="card col-12">
          <h2>Key attribution ({win === "5h" ? "5 hours" : "this week"})</h2>
          <AsyncBoundary
            status={attribution.status}
            error={attribution.error}
            isEmpty={keys.length === 0}
            emptyText="No usage in this window."
            onRetry={attribution.reload}
          >
            <div className="scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>Key</th>
                    <th>Client</th>
                    <th style={{ textAlign: "right" }}>Requests</th>
                    <th style={{ textAlign: "right" }}>Tokens (in/out)</th>
                    <th style={{ textAlign: "right" }}>CU</th>
                    <th style={{ textAlign: "right" }}>Errors</th>
                    <th>Last used</th>
                  </tr>
                </thead>
                <tbody>
                  {keys.map((k) => (
                    <tr key={k.key_id}>
                      <td className="mono">
                        {k.key_prefix ?? k.key_id}
                        {k.key_label && <div className="sub">{k.key_label}</div>}
                      </td>
                      <td>
                        {k.client_id ? (
                          <button
                            className="linkbtn"
                            onClick={() => onNavigate("clients", { clientId: k.client_id ?? undefined })}
                          >
                            {k.client_id.slice(0, 10)}
                          </button>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td className="tabular" style={{ textAlign: "right" }}>{num(k.requests)}</td>
                      <td className="tabular" style={{ textAlign: "right" }}>
                        {num(k.prompt_tokens)} / {num(k.completion_tokens)}
                      </td>
                      <td className="tabular" style={{ textAlign: "right" }}>{num(Math.round(k.cu))}</td>
                      <td className="tabular" style={{ textAlign: "right" }}>
                        {k.errors > 0 ? <Badge tone="danger">{k.errors}</Badge> : "0"}
                      </td>
                      <td className="muted tabular">{k.last_ts ? clockTime(k.last_ts) : "—"}</td>
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

function FeedBadge({ event }: { event: FeedEvent }): JSX.Element {
  if (event.type === "request.start") return <Badge tone="neutral">start</Badge>;
  if (event.type === "request.end") return <Badge tone="ok">done</Badge>;
  if (event.type === "request.error") return <Badge tone={categoryTone(event.category)}>error</Badge>;
  if (event.type === "request.rejected") return <Badge tone="warn">shed</Badge>;
  return <Badge tone="neutral">…</Badge>;
}
