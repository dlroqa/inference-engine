import { useEffect, useRef, useState, type JSX } from "react";
import {
  api,
  ApiError,
  DELIVERY_STATUSES,
  type ClientRow,
  type CreatedClientKey,
  type CreatedEndpoint,
  type DeliveryRow,
  type Reconciliation,
  type RotatedSecret,
  type WebhookEndpointRow,
} from "../lib/api";
import { useAsync, type AsyncState } from "../hooks/useAsync";
import { useConfirm } from "../hooks/useConfirm";
import { useAuthFailure } from "../hooks/useAuthScope";
import { useOneTimeSecret } from "../hooks/useOneTimeSecret";
import { AsyncBoundary } from "../components/Panel";
import { Badge, statusTone } from "../components/widgets";
import { Icon } from "../components/Icon";
import { SecretBox } from "../components/SecretBox";
import { WiredTo } from "../components/WiredTo";
import { clockTime, num } from "../lib/format";

function message(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

function csv(text: string): string[] | null {
  const items = text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return items.length > 0 ? items : null;
}

// --- Create client --------------------------------------------------------

export function CreateClientCard({ onCreated }: { onCreated: (client: ClientRow) => void }): JSX.Element {
  const [email, setEmail] = useState("");
  const [ref, setRef] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reportAuthFailure = useAuthFailure();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const client = await api.createClient({
        email: email.trim() || null,
        external_ref: ref.trim() || null,
      });
      setEmail("");
      setRef("");
      onCreated(client);
    } catch (err) {
      if (reportAuthFailure(err)) return;
      setError(message(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card col-12" data-wiring="clients.create">
      <div className="row panel-title">
        <h2>Create client</h2>
        <WiredTo id="clients.create" />
      </div>
      <form onSubmit={submit} className="row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
        <div className="field" style={{ minWidth: 220 }}>
          <label htmlFor="client-email">Email (optional)</label>
          <input id="client-email" className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} maxLength={320} autoComplete="off" />
        </div>
        <div className="field" style={{ minWidth: 220 }}>
          <label htmlFor="client-ref">Billing reference (optional)</label>
          <input id="client-ref" className="input mono" value={ref} onChange={(e) => setRef(e.target.value)} maxLength={256} aria-describedby="client-ref-help" />
          <div className="sub" id="client-ref-help">The billing provider's customer id, e.g. cus_… for Stripe.</div>
        </div>
        <button className="btn primary" type="submit" disabled={saving} style={{ marginBottom: 16 }}>
          {saving ? <span className="spinner" /> : <Icon name="users" size={16} />}
          Create client
        </button>
      </form>
      {error && (
        <div className="banner err" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}

// --- Client key (token shown once) -------------------------------------------

export function ClientKeySection({ clientId }: { clientId: string }): JSX.Element {
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const created = useOneTimeSecret<CreatedClientKey>(clientId);
  const reportAuthFailure = useAuthFailure();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const outcome = await created.run((signal) => api.createClientKey(clientId, label.trim() || null, signal));
    if (outcome.kind === "ok") setLabel("");
    if (outcome.kind === "error" && !reportAuthFailure(outcome.error)) setError(message(outcome.error));
  };

  return (
    <div data-wiring="clients.create-key">
      <div className="row panel-title" style={{ marginTop: 20 }}>
        <h3 className="detail-h">Create a client key</h3>
        <WiredTo id="clients.create-key" />
      </div>
      <form onSubmit={submit} className="row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
        <div className="field" style={{ minWidth: 220 }}>
          <label htmlFor={`client-key-label-${clientId}`}>Key label (optional)</label>
          <input
            id={`client-key-label-${clientId}`}
            className="input"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            maxLength={256}
            autoComplete="off"
          />
        </div>
        <button className="btn primary" type="submit" disabled={created.busy} style={{ marginBottom: 16 }}>
          {created.busy ? <span className="spinner" /> : <Icon name="key" size={16} />}
          Create client key
        </button>
      </form>
      {error && (
        <div className="banner err" role="alert">
          {error}
        </div>
      )}
      {created.value && (
        <SecretBox title={`Client key ${created.value.prefix}… created.`} value={created.value.token} testId="new-client-token" onDismiss={created.clear}>
          <div className="sub">It can call the inference and /client APIs for this client only.</div>
        </SecretBox>
      )}
    </div>
  );
}

// --- Reconcile ------------------------------------------------------------------

export function ReconcileSection({ clientId }: { clientId: string }): JSX.Element {
  const [result, setResult] = useState<Reconciliation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reportAuthFailure = useAuthFailure();
  const alive = useRef(true);
  useEffect(() => () => void (alive.current = false), []);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.reconcileClient(clientId);
      if (alive.current) setResult(r);
    } catch (err) {
      if (reportAuthFailure(err) || !alive.current) return;
      setError(message(err));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  return (
    <div data-wiring="clients.reconcile">
      <div className="row panel-title" style={{ marginTop: 20 }}>
        <h3 className="detail-h">Usage reconciliation</h3>
        <WiredTo id="clients.reconcile" />
      </div>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <button className="btn" type="button" onClick={run} disabled={busy}>
          {busy ? <span className="spinner" /> : <Icon name="gauge" size={16} />}
          Reconcile usage
        </button>
        {result && (
          <span className="tabular" role="status" data-testid="reconcile-result">
            5-hour window: <strong>{num(result.cu_5h)}</strong> CU · this week:{" "}
            <strong>{num(result.cu_week)}</strong> CU
          </span>
        )}
      </div>
      {error && (
        <div className="banner err" role="alert" style={{ marginTop: 12 }}>
          {error}
        </div>
      )}
    </div>
  );
}

// --- Webhook endpoints ------------------------------------------------------------

export function EndpointsSection({
  clientId,
  endpoints,
  onChanged,
}: {
  clientId: string;
  endpoints: AsyncState<{ endpoints: WebhookEndpointRow[] }>;
  onChanged: () => void;
}): JSX.Element {
  const [url, setUrl] = useState("");
  const [description, setDescription] = useState("");
  const [events, setEvents] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const created = useOneTimeSecret<CreatedEndpoint>(clientId);
  const rotated = useOneTimeSecret<RotatedSecret>(clientId);
  const [confirm, confirmDialog] = useConfirm();
  const reportAuthFailure = useAuthFailure();
  const rows = endpoints.data?.endpoints ?? [];

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    rotated.clear();
    const outcome = await created.run((signal) =>
      api.createWebhookEndpoint(
        { client_id: clientId, url: url.trim(), description: description.trim() || null, event_types: csv(events) },
        signal,
      ),
    );
    if (outcome.kind === "ok") {
      setUrl("");
      setDescription("");
      setEvents("");
      onChanged();
    }
    if (outcome.kind === "error" && !reportAuthFailure(outcome.error)) setError(message(outcome.error));
  };

  const act = async (id: string, call: () => Promise<unknown>) => {
    setActionError(null);
    setPending(id);
    try {
      await call();
      onChanged();
    } catch (err) {
      if (!reportAuthFailure(err)) setActionError(message(err));
    } finally {
      setPending(null);
    }
  };

  const toggle = async (ep: WebhookEndpointRow) => {
    if (!ep.disabled) {
      const ok = await confirm({
        title: "Disable this endpoint?",
        body: (
          <>
            <span className="mono">{ep.url}</span> stops receiving new events, and deliveries already
            queued for it are dead-lettered without being sent. Enabling it again does not resend them:
            replay them from Deliveries.
          </>
        ),
        confirmLabel: "Disable endpoint",
        wiring: "webhooks.set-disabled",
      });
      if (!ok) return;
    }
    await act(ep.id, () => api.setEndpointDisabled(ep.id, !ep.disabled));
  };

  const rotate = async (ep: WebhookEndpointRow) => {
    const ok = await confirm({
      title: "Rotate the signing secret?",
      body: (
        <>
          A new secret for <span className="mono">{ep.url}</span> is shown once. Until the grace period
          ends, deliveries are signed with both the new and the previous secret, so the receiver can
          switch without missing events.
        </>
      ),
      confirmLabel: "Rotate secret",
      tone: "primary",
      wiring: "webhooks.rotate-secret",
    });
    if (!ok) return;
    setActionError(null);
    created.clear();
    const outcome = await rotated.run((signal) => api.rotateEndpointSecret(ep.id, signal));
    if (outcome.kind === "error" && !reportAuthFailure(outcome.error)) setActionError(message(outcome.error));
  };

  const remove = async (ep: WebhookEndpointRow) => {
    const ok = await confirm({
      title: "Delete this endpoint?",
      body: (
        <>
          <span className="mono">{ep.url}</span> is removed with its signing secrets and its whole delivery
          history. This cannot be undone.
        </>
      ),
      confirmLabel: "Delete endpoint",
      wiring: "webhooks.delete",
    });
    if (!ok) return;
    await act(ep.id, () => api.deleteEndpoint(ep.id));
  };

  const rotatedEndpoint = rotated.value ? rows.find((r) => r.id === rotated.value?.id) : undefined;

  return (
    <div>
      <div className="row panel-title" style={{ marginTop: 20 }}>
        <h3 className="detail-h">Webhook endpoints</h3>
        <WiredTo id="clients.endpoints" />
      </div>
      <AsyncBoundary
        status={endpoints.status}
        error={endpoints.error}
        isEmpty={rows.length === 0}
        emptyText="No webhook endpoints."
        onRetry={endpoints.reload}
      >
        <div className="scroll">
          <table className="table">
            <thead>
              <tr>
                <th>URL</th>
                <th>Events</th>
                <th>Status</th>
                <th style={{ textAlign: "right" }}>
                  <WiredTo
                    id={["webhooks.set-disabled", "webhooks.rotate-secret", "webhooks.delete"]}
                    label="Endpoint actions"
                  />
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((ep) => (
                <tr key={ep.id}>
                  <td className="mono wrap-anywhere">
                    {ep.url}
                    {ep.description && <div className="sub">{ep.description}</div>}
                  </td>
                  <td className="muted">{ep.event_types ? ep.event_types.join(", ") : "all"}</td>
                  <td>
                    <Badge tone={ep.disabled ? "neutral" : "ok"}>{ep.disabled ? "disabled" : "enabled"}</Badge>
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <div className="row" style={{ justifyContent: "flex-end", flexWrap: "wrap" }}>
                      <button
                        className="btn"
                        type="button"
                        onClick={() => toggle(ep)}
                        disabled={pending === ep.id}
                        data-wiring="webhooks.set-disabled"
                        aria-label={`${ep.disabled ? "Enable" : "Disable"} endpoint ${ep.url}`}
                      >
                        <Icon name={ep.disabled ? "play" : "stop"} size={16} />
                        {ep.disabled ? "Enable" : "Disable"}
                      </button>
                      <button
                        className="btn"
                        type="button"
                        onClick={() => rotate(ep)}
                        disabled={rotated.busy}
                        data-wiring="webhooks.rotate-secret"
                        aria-label={`Rotate secret for endpoint ${ep.url}`}
                      >
                        <Icon name="refresh" size={16} /> Rotate secret
                      </button>
                      <button
                        className="btn danger"
                        type="button"
                        onClick={() => remove(ep)}
                        disabled={pending === ep.id}
                        data-wiring="webhooks.delete"
                        aria-label={`Delete endpoint ${ep.url}`}
                      >
                        <Icon name="trash" size={16} /> Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncBoundary>
      {actionError && (
        <div className="banner err" role="alert" style={{ marginTop: 12 }}>
          {actionError}
        </div>
      )}
      {rotated.value && (
        <SecretBox title="New signing secret." value={rotated.value.secret} testId="rotated-secret" onDismiss={rotated.clear}>
          <div className="sub" data-testid="rotation-grace">
            For <span className="mono">{rotatedEndpoint?.url ?? rotated.value.id}</span>. The previous secret
            keeps signing deliveries until {new Date(rotated.value.previous_secret_expires_at * 1000).toLocaleString()}{" "}
            ({graceText(rotated.value.grace_s)} grace period).
          </div>
        </SecretBox>
      )}

      <form onSubmit={create} data-wiring="webhooks.create" style={{ marginTop: 12 }}>
        <div className="row panel-title">
          <h3 className="detail-h">Add an endpoint</h3>
          <WiredTo id="webhooks.create" />
        </div>
        <div className="row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
          <div className="field" style={{ minWidth: 280, flex: 2 }}>
            <label htmlFor={`endpoint-url-${clientId}`}>URL</label>
            <input
              id={`endpoint-url-${clientId}`}
              className="input mono"
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
              maxLength={2048}
              aria-describedby={`endpoint-url-help-${clientId}`}
              autoComplete="off"
            />
            <div className="sub" id={`endpoint-url-help-${clientId}`}>
              http(s) only. The host must be in the engine's egress_allowlist and written in ASCII (use the
              xn-- form for an internationalized name). Redirects are never followed.
            </div>
          </div>
          <div className="field" style={{ minWidth: 200, flex: 1 }}>
            <label htmlFor={`endpoint-desc-${clientId}`}>Description (optional)</label>
            <input id={`endpoint-desc-${clientId}`} className="input" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={512} />
          </div>
          <div className="field" style={{ minWidth: 200, flex: 1 }}>
            <label htmlFor={`endpoint-events-${clientId}`}>Event types (optional)</label>
            <input
              id={`endpoint-events-${clientId}`}
              className="input mono"
              value={events}
              onChange={(e) => setEvents(e.target.value)}
              aria-describedby={`endpoint-events-help-${clientId}`}
            />
            <div className="sub" id={`endpoint-events-help-${clientId}`}>Comma-separated. Empty receives all events.</div>
          </div>
          <button className="btn primary" type="submit" disabled={created.busy} style={{ marginBottom: 16 }}>
            {created.busy ? <span className="spinner" /> : <Icon name="link" size={16} />}
            Add endpoint
          </button>
        </div>
      </form>
      {error && (
        <div className="banner err" role="alert">
          {error}
        </div>
      )}
      {created.value && (
        <SecretBox title="Endpoint added; this is its signing secret." value={created.value.secret} testId="new-endpoint-secret" onDismiss={created.clear}>
          <div className="sub">
            For <span className="mono">{created.value.url}</span>. The receiver verifies the webhook-signature
            header with it.
          </div>
        </SecretBox>
      )}
      {confirmDialog}
    </div>
  );
}

function graceText(seconds: number): string {
  if (seconds >= 86_400 && seconds % 86_400 === 0) return `${seconds / 86_400} day${seconds === 86_400 ? "" : "s"}`;
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600} hour${seconds === 3600 ? "" : "s"}`;
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} minute${seconds === 60 ? "" : "s"}`;
  return `${num(seconds)} second${seconds === 1 ? "" : "s"}`;
}

// --- Deliveries -------------------------------------------------------------------

export function DeliveriesSection({
  endpoints,
  endpointKey,
}: {
  endpoints: WebhookEndpointRow[];
  endpointKey: string;
}): JSX.Element {
  const [status, setStatus] = useState<string>("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [confirm, confirmDialog] = useConfirm();
  const reportAuthFailure = useAuthFailure();
  const endpointIds = endpoints.map((e) => e.id);
  const urlById = new Map(endpoints.map((e) => [e.id, e.url]));
  // Requested per endpoint and filtered on the server, so a busy engine's other
  // clients can never push this client's deliveries off the page.
  const deliveries = useAsync(async () => {
    const pages = await Promise.all(
      endpointIds.map((id) => api.listDeliveries({ endpoint_id: id, limit: 100, ...(status ? { status } : {}) })),
    );
    return pages.flatMap((p) => p.deliveries).sort((a, b) => b.created_at - a.created_at);
  }, [endpointKey, status]);
  const rows: DeliveryRow[] = deliveries.data ?? [];

  const replay = async (d: DeliveryRow) => {
    const ok = await confirm({
      title: "Replay this delivery?",
      body: (
        <>
          <span className="mono">{d.event_type}</span> is queued again for{" "}
          <span className="mono">{urlById.get(d.endpoint_id) ?? d.endpoint_id}</span> with its attempts reset.
          The receiver may get the event twice unless it de-duplicates by the webhook-id header.
        </>
      ),
      confirmLabel: "Replay delivery",
      tone: "primary",
      wiring: "webhooks.replay",
    });
    if (!ok) return;
    setActionError(null);
    setPending(d.id);
    try {
      await api.replayDelivery(d.id);
      deliveries.reload();
    } catch (err) {
      if (!reportAuthFailure(err)) setActionError(message(err));
    } finally {
      setPending(null);
    }
  };

  return (
    <div>
      <div className="row spread" style={{ marginTop: 20, flexWrap: "wrap" }}>
        <div className="row panel-title">
          <h3 className="detail-h">Recent deliveries</h3>
          <WiredTo id="clients.deliveries" />
        </div>
        <div className="row">
          <label htmlFor={`delivery-status-${endpointKey}`} className="sub">
            Status
          </label>
          <select
            id={`delivery-status-${endpointKey}`}
            className="input"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            data-wiring="clients.deliveries"
          >
            <option value="">All</option>
            {DELIVERY_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <WiredTo id="clients.deliveries" label="Status filter" />
          <button className="btn" type="button" onClick={deliveries.reload} data-wiring="clients.deliveries">
            <Icon name="refresh" size={16} /> Refresh
          </button>
        </div>
      </div>
      <AsyncBoundary
        status={deliveries.status}
        error={deliveries.error}
        isEmpty={rows.length === 0}
        emptyText={status ? `No ${status} deliveries.` : "No deliveries."}
        onRetry={deliveries.reload}
      >
        <div className="scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Event</th>
                <th>Status</th>
                <th style={{ textAlign: "right" }}>Attempts</th>
                <th>Last result</th>
                <th>Created</th>
                <th style={{ textAlign: "right" }}>
                  <WiredTo id="webhooks.replay" label="Replay" />
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((d) => (
                <tr key={d.id} data-delivery-id={d.id}>
                  <td className="mono">{d.event_type}</td>
                  <td>
                    <Badge tone={statusTone(d.status)}>{d.status}</Badge>
                  </td>
                  <td className="tabular" style={{ textAlign: "right" }}>{d.attempts}</td>
                  <td className="muted">{d.last_status_code ?? (d.last_error ? "error" : "—")}</td>
                  <td className="muted tabular">{clockTime(d.created_at)}</td>
                  <td style={{ textAlign: "right" }}>
                    <button
                      className="btn"
                      type="button"
                      onClick={() => replay(d)}
                      disabled={pending === d.id || d.status === "pending"}
                      data-wiring="webhooks.replay"
                      aria-label={`Replay ${d.event_type} delivery ${d.id.slice(0, 8)}`}
                    >
                      <Icon name="play" size={16} /> Replay
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncBoundary>
      {actionError && (
        <div className="banner err" role="alert" style={{ marginTop: 12 }}>
          {actionError}
        </div>
      )}
      {confirmDialog}
    </div>
  );
}
