import { useEffect, useMemo, useState, type JSX } from "react";
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

export function Logs({ initialQuery }: { initialQuery?: string } = {}): JSX.Element {
  const [level, setLevel] = useState("");
  const [query, setQuery] = useState(initialQuery ?? "");

  // Apply a cross-linked filter (e.g. a request id from Monitoring) when it changes.
  useEffect(() => {
    if (initialQuery !== undefined) setQuery(initialQuery);
  }, [initialQuery]);
  const { status, data, error, reload } = useAsync(
    () => api.logs({ limit: 200, level: level || undefined }),
    [level],
  );

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
