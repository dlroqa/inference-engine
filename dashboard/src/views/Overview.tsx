import type { JSX } from "react";
import { useLiveMetrics } from "../hooks/useLiveMetrics";
import { useLiveFeed } from "../hooks/useLiveFeed";
import { StatCard, Meter, Badge, stateTone, categoryTone } from "../components/widgets";
import { ConnBadge } from "../components/Panel";
import { bytes, duration, num, clockTime } from "../lib/format";
import { api, type EnergyPanel, type FeedEvent, type SchedulerPanel } from "../lib/api";
import { useAsync } from "../hooks/useAsync";
import { WiredTo } from "../components/WiredTo";
import { liveView, staleNotice, type LiveView } from "../lib/liveState";

// The stat cards read the live metrics stream, or the polling fallback when the
// stream is unavailable.
const METRICS_HINT = ["overview.metrics", "overview.metrics-fallback"];

// Shown instead of numbers until the first snapshot arrives. It says nothing
// about any probe: no data is not evidence that something is unavailable.
const NO_DATA = "—";

function energyText(energy: EnergyPanel): string {
  if (energy.state !== "measured") return "unavailable";
  const w = energy.watts !== null ? `${energy.watts.toFixed(1)} W` : "—";
  const j = energy.j_per_token !== null ? ` · ${energy.j_per_token} J/tok` : "";
  return `${w}${j}`;
}

// Energy counts as measured only when the engine says so; a source name such as
// "rapl" alone (e.g. while it is still establishing a baseline) is not a measurement.
function energySub(energy: EnergyPanel): string {
  if (energy.state === "measured") return energy.source ? `source: ${energy.source}` : "measured";
  return `Not measured — ${energy.reason || "no validated power probe"}`;
}

function waitText(ms: number): string {
  return `${num(Math.round(ms))} ms`;
}

function SchedulerFacts({ s }: { s: SchedulerPanel }): JSX.Element {
  const inUsePct = s.max_concurrency > 0 ? (s.in_use / s.max_concurrency) * 100 : null;
  // A zero-capacity queue means queueing is off (requests beyond concurrency are
  // rejected at once), not a full queue; never divide by it.
  const queuePct = s.max_queue_depth > 0 ? (s.queue_depth / s.max_queue_depth) * 100 : null;
  return (
    <>
      <div className="sched-meters">
        <Meter
          label="In use"
          percent={inUsePct}
          detail={`${num(s.in_use)} / ${num(s.max_concurrency)} slots`}
        />
        {queuePct === null ? (
          <div className="meter">
            <div className="head">
              <span>Queue</span>
              <span className="tabular muted">queueing disabled</span>
            </div>
            <div className="sub">{num(s.queue_depth)} waiting · max queue depth 0</div>
          </div>
        ) : (
          <Meter
            label="Queue"
            percent={queuePct}
            detail={`${num(s.queue_depth)} / ${num(s.max_queue_depth)} waiting`}
          />
        )}
      </div>
      <dl className="kv tabular">
        <div>
          <dt>Admitted</dt>
          <dd>{num(s.admitted_total)}</dd>
        </div>
        <div>
          <dt>Rejected</dt>
          <dd>
            {num(s.rejected_total)}
            <span className="muted">
              {" "}
              (queue full {num(s.rejected_queue_full)} · timeout {num(s.rejected_timeout)})
            </span>
          </dd>
        </div>
        <div>
          <dt>Cancelled</dt>
          <dd>{num(s.cancelled_total)}</dd>
        </div>
        <div>
          <dt>Wait (avg / max / last)</dt>
          <dd>
            {s.wait_ms_avg === null
              ? "no waits yet"
              : `${waitText(s.wait_ms_avg)} / ${waitText(s.wait_ms_max)} / ${waitText(s.wait_ms_last)}`}
          </dd>
        </div>
      </dl>
    </>
  );
}

function SchedulerCard({ view, s }: { view: LiveView; s: SchedulerPanel | null | undefined }): JSX.Element {
  let body: JSX.Element;
  if (view.kind === "loading") {
    body = (
      <div className="empty row" style={{ justifyContent: "center" }}>
        <span className="spinner" role="status" aria-label="Loading scheduler state" /> Waiting for metrics…
      </div>
    );
  } else if (view.kind === "unavailable") {
    body = <div className="empty">Scheduler state unavailable — metrics are disconnected; reconnecting…</div>;
  } else if (!s) {
    body = <div className="empty">Scheduler not running.</div>;
  } else {
    body = <SchedulerFacts s={s} />;
  }
  return (
    <section className="card col-12" aria-labelledby="scheduler-heading" data-testid="scheduler-card">
      <div className="row panel-title">
        <h2 id="scheduler-heading">Scheduler</h2>
        <WiredTo id={METRICS_HINT} label="Scheduler" />
      </div>
      {body}
    </section>
  );
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
  } else if (e.type === "request.rejected") {
    body = (
      <>
        <Badge tone="warn">shed</Badge>
        <span className="muted">{e.reason}</span>
        {e.retry_after_s != null && <span>retry {e.retry_after_s}s</span>}
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
  const { snapshot, status, fresh } = useLiveMetrics();
  const view = liveView(status, snapshot, fresh);
  const notice = staleNotice(view);
  const { events, status: feedStatus } = useLiveFeed();
  const overview = useAsync(() => api.overview(), []);
  const ov = overview.data;

  const c = snapshot?.counters;
  const r = snapshot?.resources;
  const model = snapshot?.backend;
  const s = snapshot?.scheduler;

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
          {ov && (
            <span className="sub" data-testid="engine-readiness">
              v{ov.version} · {ov.ready ? "ready" : "not ready"}
              {!ov.ready &&
                ` (${Object.entries(ov.checks)
                  .filter(([, v]) => v !== "ok" && v !== "applied")
                  .map(([k, v]) => `${k}: ${v}`)
                  .join(", ")})`}
            </span>
          )}
          {overview.status === "error" && (
            <span className="sub" role="alert">
              readiness unavailable: {overview.error}
            </span>
          )}
          <WiredTo id="overview.readiness" />
        </div>
        <div className="row">
          <ConnBadge status={status} />
          <WiredTo id={["overview.metrics", "overview.metrics-fallback"]} />
        </div>
      </div>

      {notice && (
        <div className="banner info" role="status" data-testid="metrics-stale">
          {notice}
        </div>
      )}
      {view.kind === "unavailable" && (
        <div className="banner err" role="status" data-testid="metrics-unavailable">
          Live metrics are unavailable — disconnected; reconnecting…
        </div>
      )}

      <div className="grid">
        <div className="col-3">
          <StatCard
            label="Requests"
            help={<WiredTo id={METRICS_HINT} label="Requests card" />}
            value={c ? num(c.requests_total) : NO_DATA}
            sub={
              c
                ? `${num(c.requests_active)} active · ` +
                  (s ? `${num(s.queue_depth)} queued · ` : "") +
                  `${num(c.requests_errors)} errors` +
                  (s && s.rejected_total > 0 ? ` · ${num(s.rejected_total)} shed` : "")
                : undefined
            }
          />
        </div>
        <div className="col-3">
          <StatCard
            label="Tokens"
            help={<WiredTo id={METRICS_HINT} label="Tokens card" />}
            value={c ? num(c.prompt_tokens_total + c.completion_tokens_total) : NO_DATA}
            sub={c ? `${num(c.prompt_tokens_total)} in · ${num(c.completion_tokens_total)} out` : undefined}
          />
        </div>
        <div className="col-3">
          <StatCard
            label="Uptime"
            help={<WiredTo id={METRICS_HINT} label="Uptime card" />}
            value={snapshot ? duration(snapshot.uptime_s) : NO_DATA}
            sub={c ? `${num(c.requests_per_min)} req/min` : undefined}
          />
        </div>
        <div className="col-3">
          <StatCard
            label="Energy"
            help={<WiredTo id={METRICS_HINT} label="Energy card" />}
            value={snapshot ? energyText(snapshot.energy) : NO_DATA}
            sub={snapshot ? energySub(snapshot.energy) : undefined}
          />
        </div>

        <SchedulerCard view={view} s={s} />

        <div className="card col-4">
          <div className="row panel-title">
            <h2>Resources</h2>
            <WiredTo id="overview.metrics" label="Resources" />
          </div>
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
            <div className="row panel-title">
              <h2 style={{ margin: 0 }}>Inference live feed</h2>
              <WiredTo id="overview.feed" />
            </div>
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
