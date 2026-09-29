import { useId, useState, type FormEvent, type JSX } from "react";
import {
  api,
  type BackendRow,
  type BackendsPage,
  type RouteFeature,
  type RoutePlan,
  type RouteRow,
  type RoutesPage,
} from "../lib/api";
import { useAsync, type AsyncState } from "../hooks/useAsync";
import { useSystem } from "../hooks/useSystem";
import { useAuthFailure } from "../hooks/useAuthScope";
import { AsyncBoundary } from "../components/Panel";
import { Badge, stateTone } from "../components/widgets";
import { Icon } from "../components/Icon";
import { WiredTo } from "../components/WiredTo";
import { docsUrl } from "../lib/wiring";
import { num, pct } from "../lib/format";
import { describeActionError } from "../lib/switches";

// Backends & Routing: the pool, per-route measurements and the configured
// routing summary, plus a route-plan dry run. Everything here is read-only;
// routing is configured in config.toml on the engine.

const DASH = "—";

function ms(v: number | null): string {
  return v === null ? DASH : `${num(Math.round(v))} ms`;
}

function counts(map: Record<string, number>): string {
  const entries = Object.entries(map);
  return entries.length === 0 ? DASH : entries.map(([k, n]) => `${k} ×${num(n)}`).join(", ");
}

function YesNo({ value, yes = "Yes", no = "No" }: { value: boolean; yes?: string; no?: string }): JSX.Element {
  return (
    <span className="row" style={{ gap: 4 }}>
      <Icon name={value ? "check" : "x"} size={14} />
      {value ? yes : no}
    </span>
  );
}

function modelCell(b: BackendRow): string {
  if (b.model_id) return b.model_id;
  if (b.served_model) return `${b.served_model} (configured)`;
  return DASH;
}

function cacheCell(b: BackendRow): string {
  const parts = [b.supports_prefix_cache ? "prefix cache" : "no prefix cache"];
  if (b.cache && Object.keys(b.cache).length > 0) {
    parts.push(Object.entries(b.cache).map(([k, v]) => `${k} ${num(v)}`).join(", "));
  } else if (b.supports_kv_cache_metrics) {
    parts.push("no cache stats yet");
  }
  return parts.join(" · ");
}

function BackendPool({ state }: { state: AsyncState<BackendsPage> }): JSX.Element {
  const { status, data, error, reload } = state;
  const rows = data?.backends ?? [];
  return (
    <div className="card col-12" data-wiring="routing.backends">
      <div className="row panel-title">
        <h2>Backend pool</h2>
        <WiredTo id="routing.backends" label="Backend pool" />
      </div>
      <AsyncBoundary
        status={status}
        error={error}
        isEmpty={rows.length === 0}
        emptyText="No backends are configured in the pool."
        onRetry={reload}
      >
        {data && (
          <p className="sub" style={{ marginTop: 0 }}>
            {num(data.count)} {data.count === 1 ? "backend" : "backends"} · pool{" "}
            {data.ready ? "can serve (at least one backend ready)" : "cannot serve (no backend ready)"}
          </p>
        )}
        <div className="scroll" role="region" aria-label="Backend pool table" tabIndex={0}>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Backend</th>
                <th scope="col">Location</th>
                <th scope="col">Tier</th>
                <th scope="col">State</th>
                <th scope="col">Available</th>
                <th scope="col">In flight</th>
                <th scope="col">Model</th>
                <th scope="col">Context</th>
                <th scope="col">Cache</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <tr key={b.name}>
                  <td>
                    <span className="mono">{b.name}</span>
                    <div className="muted">{b.kind}</div>
                  </td>
                  <td>
                    {b.location}
                    {b.external && <div className="muted">external provider</div>}
                  </td>
                  <td>{b.tier}</td>
                  <td>
                    <Badge tone={stateTone(b.state)}>{b.state}</Badge>
                  </td>
                  <td>
                    <YesNo value={b.available} />
                  </td>
                  <td className="tabular">
                    {num(b.in_flight)} / {num(b.max_in_flight)}
                  </td>
                  <td className="mono wrap-anywhere">{modelCell(b)}</td>
                  <td className="tabular">{b.context_length !== null ? num(b.context_length) : DASH}</td>
                  <td className="muted">{cacheCell(b)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncBoundary>
    </div>
  );
}

function RouteMetrics({ state }: { state: AsyncState<RoutesPage> }): JSX.Element {
  const { status, data, error, reload } = state;
  const rows: RouteRow[] = data?.routes ?? [];
  const sheds = data ? Object.entries(data.sheds) : [];
  return (
    <div className="card col-12" data-wiring="routing.routes">
      <div className="row panel-title">
        <h2>Per-route cost and latency</h2>
        <WiredTo id="routing.routes" label="Per-route cost and latency" />
      </div>
      <p className="sub" style={{ marginTop: 0 }}>
        Recorded in memory since the engine started. Latencies are averages (no percentiles are
        collected); {DASH} means no samples yet.
      </p>
      <AsyncBoundary
        status={status}
        error={error}
        isEmpty={rows.length === 0 && sheds.length === 0}
        emptyText="No routed requests yet."
        onRetry={reload}
      >
        {rows.length > 0 && (
          <div className="scroll" role="region" aria-label="Per-route table" tabIndex={0}>
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Model</th>
                  <th scope="col">Backend</th>
                  <th scope="col">Tier</th>
                  <th scope="col">Requests</th>
                  <th scope="col">Errors</th>
                  <th scope="col">Cancelled</th>
                  <th scope="col">Success</th>
                  <th scope="col">Avg total</th>
                  <th scope="col">Avg first token</th>
                  <th scope="col">Avg queue wait</th>
                  <th scope="col">Avg output</th>
                  <th scope="col">Tokens in / out</th>
                  <th scope="col">Cost</th>
                  <th scope="col">Route reasons</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={`${r.model}|${r.backend}`}>
                    <td className="mono wrap-anywhere">{r.model}</td>
                    <td className="mono">{r.backend}</td>
                    <td>{r.tier ?? DASH}</td>
                    <td className="tabular">{num(r.requests)}</td>
                    <td className="tabular">{num(r.errors)}</td>
                    <td className="tabular">{num(r.cancelled)}</td>
                    <td className="tabular">{r.success_rate === null ? DASH : pct(r.success_rate * 100)}</td>
                    <td className="tabular">{ms(r.avg_total_ms)}</td>
                    <td className="tabular">{ms(r.avg_ttft_ms)}</td>
                    <td className="tabular">{ms(r.avg_queue_wait_ms)}</td>
                    <td className="tabular">
                      {r.avg_output_tps === null ? DASH : `${r.avg_output_tps.toFixed(1)} tok/s`}
                    </td>
                    <td className="tabular">
                      {num(r.prompt_tokens)} / {num(r.completion_tokens)}
                    </td>
                    <td className="tabular">{r.cost.toLocaleString(undefined, { maximumFractionDigits: 6 })}</td>
                    <td className="muted">{counts(r.reasons)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {data && (
          <p className="sub tabular" style={{ marginBottom: 0 }}>
            Totals: {num(data.totals.requests)} requests · {num(data.totals.errors)} errors ·{" "}
            {num(data.totals.cancelled)} cancelled · cost{" "}
            {data.totals.cost.toLocaleString(undefined, { maximumFractionDigits: 6 })} ·{" "}
            {num(data.totals.sheds)} never admitted (shed)
            {sheds.length > 0 && ` — ${counts(data.sheds)}`}
          </p>
        )}
      </AsyncBoundary>
    </div>
  );
}

function ConfiguredRouting(): JSX.Element {
  const sys = useSystem();
  const routing = sys.info?.routing ?? null;
  const status = sys.info ? "ready" : sys.status === "loading" ? "loading" : "error";
  return (
    <div className="card col-6" data-wiring="app.system">
      <div className="row panel-title">
        <h2>Configured routing</h2>
        <WiredTo id="app.system" label="Configured routing" />
      </div>
      <AsyncBoundary
        status={status}
        error={`Routing configuration could not be loaded (${sys.error ?? "unavailable"}).`}
        onRetry={sys.refresh}
      >
        {routing && (
          <dl className="kv kv-rows">
            <div>
              <dt>Primary backend</dt>
              <dd className="mono">{routing.backend_kind}</dd>
            </div>
            <div>
              <dt>Virtual models</dt>
              <dd>
                {routing.virtual_models.length === 0 ? (
                  "none"
                ) : (
                  <ul className="plain-list">
                    {routing.virtual_models.map((v) => (
                      <li key={v.name}>
                        <span className="mono">{v.name}</span> <span className="muted">· {v.policy}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </dd>
            </div>
            <div>
              <dt>Workload routing</dt>
              <dd>
                {routing.workload_routing_enabled ? "on" : "off"} · {num(routing.workload_rule_count)}{" "}
                {routing.workload_rule_count === 1 ? "rule" : "rules"}
              </dd>
            </div>
            <div>
              <dt>Remote workers</dt>
              <dd className="mono wrap-anywhere">
                {routing.remote_workers.length ? routing.remote_workers.join(", ") : "none"}
              </dd>
            </div>
            <div>
              <dt>Spillover providers</dt>
              <dd className="mono wrap-anywhere">
                {routing.spillover_providers.length ? routing.spillover_providers.join(", ") : "none"}
              </dd>
            </div>
          </dl>
        )}
      </AsyncBoundary>
    </div>
  );
}

function PlanResult({ plan, features }: { plan: RoutePlan; features: RouteFeature[] }): JSX.Element {
  const total = plan.steps.length;
  return (
    <div data-testid="route-plan-result">
      <p style={{ marginBottom: 8 }}>
        {plan.chosen !== null ? (
          <>
            <Icon name="check" size={16} /> Would choose <strong className="mono">{plan.chosen}</strong>
            {total > 1 && plan.chosen_step !== null && ` (step ${plan.chosen_step + 1} of ${total})`}
          </>
        ) : (
          <>
            <Icon name="alert" size={16} /> No backend could take this request right now
          </>
        )}
      </p>
      <dl className="kv kv-rows">
        <div>
          <dt>Model</dt>
          <dd>
            <span className="mono wrap-anywhere">{plan.model}</span>
            {!plan.known && <div className="muted">Not a known model or virtual model name.</div>}
          </dd>
        </div>
        <div>
          <dt>Policy</dt>
          <dd className="mono">{plan.policy}</dd>
        </div>
        <div>
          <dt>Required features</dt>
          <dd className="mono">{features.length ? features.join(", ") : "none"}</dd>
        </div>
        <div>
          <dt>Workload rule</dt>
          <dd>
            {plan.workload_rule === null ? (
              "none matched"
            ) : (
              <>
                <span className="mono">{plan.workload_rule}</span>
                {plan.workload_rule_preferred_targets && (
                  <span className="muted"> · prefers {plan.workload_rule_preferred_targets.join(", ")}</span>
                )}
              </>
            )}
          </dd>
        </div>
      </dl>
      {plan.steps.map((step, i) => (
        <div key={i} style={{ marginTop: 12 }}>
          <h3 className="detail-h">
            {total > 1 ? `Step ${i + 1} of ${total}: ` : "Candidates in "}
            <span className="mono">{step.targets.includes("*all*") ? "whole pool" : step.targets.join(", ")}</span>
          </h3>
          {step.candidates.length === 0 ? (
            <p className="muted" style={{ margin: 0 }}>
              No eligible backend in this scope.
            </p>
          ) : (
            <div className="scroll" role="region" aria-label={`Step ${i + 1} candidates`} tabIndex={0}>
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Backend</th>
                    <th scope="col">State</th>
                    <th scope="col">Available</th>
                    <th scope="col">In flight</th>
                    <th scope="col">Has capacity</th>
                    <th scope="col">Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {step.candidates.map((c) => (
                    <tr key={c.name}>
                      <td className="mono">{c.name}</td>
                      <td>
                        <Badge tone={stateTone(c.state)}>{c.state}</Badge>
                      </td>
                      <td>
                        <YesNo value={c.available} />
                      </td>
                      <td className="tabular">{num(c.in_flight)}</td>
                      <td>
                        <YesNo value={c.has_capacity} />
                      </td>
                      <td>{step.chosen === c.name ? <strong>chosen</strong> : <span className="muted">{DASH}</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function RoutePlanForm({ suggestions }: { suggestions: string[] }): JSX.Element {
  const reportAuthFailure = useAuthFailure();
  const ids = useId();
  const [model, setModel] = useState("");
  const [structured, setStructured] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ plan: RoutePlan; features: RouteFeature[] } | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const name = model.trim();
    if (!name) return;
    const features: RouteFeature[] = structured ? ["structured_output"] : [];
    setBusy(true);
    setError(null);
    try {
      const plan = await api.routePlan(name, features);
      setResult({ plan, features });
    } catch (err) {
      if (reportAuthFailure(err)) return;
      setResult(null);
      setError(`The dry run failed: ${describeActionError(err)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card col-6" data-wiring="routing.plan">
      <div className="row panel-title">
        <h2>Route plan (dry run)</h2>
        <WiredTo id="routing.plan" label="Route plan (dry run)" />
      </div>
      <p className="sub" style={{ marginTop: 0 }} id={`${ids}-help`}>
        Shows which backend the router would choose for a model right now. Only the model name and
        required features are sent, never a prompt, so prompt-prefix (cache) affinity is not
        simulated. Nothing is reserved or generated.
      </p>
      <form onSubmit={submit} aria-describedby={`${ids}-help`}>
        <div className="field">
          <label htmlFor={`${ids}-model`}>Model name</label>
          <input
            id={`${ids}-model`}
            className="input"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            list={`${ids}-models`}
            autoComplete="off"
            spellCheck={false}
          />
          <datalist id={`${ids}-models`}>
            {suggestions.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
        </div>
        <label className="row check-row">
          <input type="checkbox" checked={structured} onChange={(e) => setStructured(e.target.checked)} />
          Requires structured output (JSON / grammar)
        </label>
        <div className="row" style={{ marginTop: 12 }}>
          <button className="btn primary" type="submit" disabled={busy || !model.trim()}>
            {busy ? <span className="spinner" /> : <Icon name="route" size={16} />}
            Plan route
          </button>
          <WiredTo id="routing.plan" label="Plan route" />
        </div>
      </form>
      <div aria-live="polite" style={{ marginTop: 12 }}>
        {error && (
          <div className="banner err" role="alert" style={{ marginBottom: 0 }}>
            {error}
          </div>
        )}
        {result && <PlanResult plan={result.plan} features={result.features} />}
      </div>
    </div>
  );
}

export function Routing(): JSX.Element {
  const sys = useSystem();
  const backends = useAsync(() => api.backends(), []);
  const routes = useAsync(() => api.routes(), []);

  const suggestions = Array.from(
    new Set([
      ...(backends.data?.backends ?? []).flatMap((b) => [b.model_id, b.served_model]),
      ...(sys.info?.routing.virtual_models ?? []).map((v) => v.name),
    ]),
  ).filter((s): s is string => Boolean(s));

  const refresh = () => {
    backends.reload();
    routes.reload();
    sys.refresh();
  };

  return (
    <>
      <div className="topbar">
        <h1>Backends &amp; routing</h1>
        <div className="row" data-wiring="routing.backends routing.routes app.system">
          <button className="btn" type="button" onClick={refresh}>
            <Icon name="refresh" size={16} /> Refresh
          </button>
          <WiredTo id={["routing.backends", "routing.routes", "app.system"]} label="Refresh" />
        </div>
      </div>

      <div className="banner info row" style={{ gap: 8, flexWrap: "wrap" }} data-wiring="local.docs-link">
        <Icon name="info" size={16} />
        <span>
          Read-only. Backends, virtual models and routing policies are configured in{" "}
          <span className="mono">config.toml</span> and take effect when the engine starts.
        </span>
        <a
          href={docsUrl({ ref: "docs/backends.md", title: "Backends" })}
          target="_blank"
          rel="noopener noreferrer"
        >
          Backends and routing documentation (opens in a new tab)
        </a>
        <WiredTo id="local.docs-link" label="Documentation link" />
      </div>

      <div className="grid">
        <BackendPool state={backends} />
        <RouteMetrics state={routes} />
        <ConfiguredRouting />
        <RoutePlanForm suggestions={suggestions} />
      </div>
    </>
  );
}
