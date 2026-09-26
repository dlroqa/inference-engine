import { useMemo, useState, type JSX } from "react";
import { api, type LogEvent } from "../lib/api";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/Panel";
import { Badge, categoryTone } from "../components/widgets";
import { Icon } from "../components/Icon";
import { clockTime } from "../lib/format";

const LEVELS = ["", "ERROR", "WARNING", "INFO"];

function levelTone(level: string): "danger" | "warn" | "neutral" {
  if (level === "ERROR" || level === "CRITICAL") return "danger";
  if (level === "WARNING") return "warn";
  return "neutral";
}

type NavFn = (view: string, opts?: { logQuery?: string }) => void;

export function Logs({
  requestId,
  onNavigate,
}: { requestId?: string; onNavigate?: NavFn } = {}): JSX.Element {
  const [level, setLevel] = useState("");
  const [query, setQuery] = useState("");

  // A request id (e.g. linked from Monitoring, or in the URL) is filtered on the
  // server, so it finds the request's events however old they are — not only
  // within the most recent page.
  const { status, data, error, reload } = useAsync(() => {
    const params: { limit: number; level?: string; request_id?: string } = { limit: 200 };
    if (level) params.level = level;
    if (requestId) params.request_id = requestId;
    return api.logs(params);
  }, [level, requestId]);

  const rows = useMemo(() => {
    const events = data?.events ?? [];
    if (!query.trim()) return events;
    const q = query.toLowerCase();
    return events.filter(
      (e) =>
        (e.request_id ?? "").toLowerCase().includes(q) ||
        (e.model ?? "").toLowerCase().includes(q) ||
        (e.event ?? "").toLowerCase().includes(q) ||
        (e.category ?? "").toLowerCase().includes(q),
    );
  }, [data, query]);

  return (
    <>
      <div className="topbar">
        <h1>Logs</h1>
        <button className="btn" onClick={reload}>
          <Icon name="refresh" size={16} /> Refresh
        </button>
      </div>

      <div className="card col-12">
        {requestId && (
          <div className="banner info row spread" role="status">
            <span>
              Showing events for request <span className="mono">{requestId}</span>
            </span>
            <button className="btn" onClick={() => onNavigate?.("logs")}>
              Clear request filter
            </button>
          </div>
        )}
        <div className="row" style={{ marginBottom: 16 }}>
          <div className="field" style={{ margin: 0 }}>
            <label htmlFor="lvl">Level</label>
            <select id="lvl" className="input" value={level} onChange={(e) => setLevel(e.target.value)}>
              {LEVELS.map((l) => (
                <option key={l || "all"} value={l}>
                  {l || "All"}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ margin: 0, flex: 1, minWidth: 200 }}>
            <label htmlFor="q">Filter</label>
            <input
              id="q"
              className="input"
              placeholder="request id, model, category…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        </div>

        <AsyncBoundary
          status={status}
          error={error}
          isEmpty={rows.length === 0}
          emptyText="No matching log events."
          onRetry={reload}
        >
          <div className="scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Level</th>
                  <th>Event</th>
                  <th>Category</th>
                  <th>Stage</th>
                  <th>Model</th>
                  <th>Request</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((e: LogEvent, i) => (
                  <tr key={`${e.request_id}-${e.ts}-${i}`}>
                    <td className="tabular">{clockTime(e.ts)}</td>
                    <td>
                      <Badge tone={levelTone(e.level)}>{e.level}</Badge>
                    </td>
                    <td>
                      {e.event}
                      {e.detail && <div className="sub">{e.detail}</div>}
                    </td>
                    <td>{e.category ? <Badge tone={categoryTone(e.category)}>{e.category}</Badge> : "—"}</td>
                    <td className="muted">{e.stage ?? "—"}</td>
                    <td className="mono muted">{e.model ?? "—"}</td>
                    <td className="mono muted">{e.request_id ? e.request_id.slice(0, 16) : "—"}</td>
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
