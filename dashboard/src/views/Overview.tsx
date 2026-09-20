import type { JSX } from "react";
import { useLiveMetrics } from "../hooks/useLiveMetrics";
import { useLiveFeed } from "../hooks/useLiveFeed";
import { StatCard, Meter, Badge, stateTone, categoryTone } from "../components/widgets";
import { ConnBadge } from "../components/Panel";
import { bytes, duration, num, clockTime } from "../lib/format";
import type { FeedEvent } from "../lib/api";

function energyText(energy: { state: string; watts: number | null; j_per_token: number | null }): string {
  if (energy.state !== "measured") return "unavailable";
  const w = energy.watts !== null ? `${energy.watts.toFixed(1)} W` : "—";
  const j = energy.j_per_token !== null ? ` · ${energy.j_per_token} J/tok` : "";
  return `${w}${j}`;
}

function feedLine(e: FeedEvent): JSX.Element {
  const rid = e.request_id ? e.request_id.slice(0, 16) : "";
  let body: JSX.Element;
  if (e.type === "request.start") {
    body = (
      <>
        <Badge tone="neutral">start</Badge>
        <span className="mono">{e.model ?? "?"}</span>
        <span className="muted">{e.endpoint}</span>
      </>
    );
  } else if (e.type === "request.end") {
    body = (
      <>
        <Badge tone="ok">done</Badge>
        <span>
          {num(e.completion_tokens)} tok
          {e.total_ms != null ? ` · ${e.total_ms} ms` : ""}
        </span>
        <span className="muted">{e.finish_reason}</span>
      </>
    );
  } else if (e.type === "request.error") {
    body = (
      <>
        <Badge tone={categoryTone(e.category)}>error</Badge>
        <span className="muted">{e.category}</span>
      </>
    );
  } else {
    body = (
      <>
        <Badge tone="neutral">…</Badge>
        <span>{num(e.tokens)} tok</span>
      </>
    );
  }
  return (
    <div className="feed-row" key={`${e.request_id}-${e.type}-${e.ts}`}>
      {body}
      <span className="rid mono">{rid}</span>
      <time>{clockTime(e.ts)}</time>
    </div>
  );
}

export function Overview(): JSX.Element {
  const { snapshot, status } = useLiveMetrics();
  const { events, status: feedStatus } = useLiveFeed();

  const c = snapshot?.counters;
  const r = snapshot?.resources;
  const model = snapshot?.backend;

  return (
    <>
      <div className="topbar">
        <div className="row">
          <h1>Overview</h1>
          {model && (
            <Badge tone={stateTone(model.state)}>
              {model.model_id ?? "no model"} · {model.state}
            </Badge>
          )}
        </div>
        <ConnBadge status={status} />
      </div>

      <div className="grid">
        <div className="col-3">
          <StatCard
            label="Requests"
            value={num(c?.requests_total ?? 0)}
            sub={`${num(c?.requests_active ?? 0)} active · ${num(c?.requests_errors ?? 0)} errors`}
          />
        </div>
        <div className="col-3">
          <StatCard
            label="Tokens"
            value={num((c?.prompt_tokens_total ?? 0) + (c?.completion_tokens_total ?? 0))}
            sub={`${num(c?.prompt_tokens_total ?? 0)} in · ${num(c?.completion_tokens_total ?? 0)} out`}
          />
        </div>
        <div className="col-3">
          <StatCard label="Uptime" value={duration(snapshot?.uptime_s)} sub={`${num(c?.requests_per_min ?? 0)} req/min`} />
        </div>
        <div className="col-3">
          <StatCard
            label="Energy"
            value={snapshot ? energyText(snapshot.energy) : "—"}
            sub={snapshot?.energy.state === "measured" ? snapshot.energy.source ?? "" : "no validated probe"}
          />
        </div>

        <div className="card col-4">
          <h2>Resources</h2>
          <Meter label="CPU" percent={r?.cpu_percent ?? null} />
          <Meter label="Memory" percent={r?.memory.percent ?? null} detail={r ? `${bytes(r.memory.used)} / ${bytes(r.memory.total)}` : undefined} />
          <Meter label="Disk" percent={r?.disk.percent ?? null} detail={r ? `${bytes(r.disk.free)} free` : undefined} />
          <div className="sub" style={{ marginTop: 12 }}>
            Process RSS {bytes(r?.process_rss)} · GPU{" "}
            {snapshot?.gpu.available ? "present" : "unavailable"}
          </div>
        </div>

        <div className="card col-8">
          <div className="row spread" style={{ marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>Inference live feed</h2>
            <ConnBadge status={feedStatus} />
          </div>
          {feedStatus === "down" && (
            <div className="banner info">Feed disconnected — reconnecting…</div>
          )}
          <div className="scroll">
            {events.length === 0 ? (
              <div className="empty">No inference activity yet.</div>
            ) : (
              [...events].reverse().map(feedLine)
            )}
          </div>
        </div>
      </div>
    </>
  );
}
