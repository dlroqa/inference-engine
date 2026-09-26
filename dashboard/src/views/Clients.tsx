import { useEffect, useMemo, useState, type JSX } from "react";
import { api, type KeyAttribution } from "../lib/api";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/Panel";
import { Badge, statusTone } from "../components/widgets";
import { Icon } from "../components/Icon";
import { WiredTo } from "../components/WiredTo";
import { num, clockTime } from "../lib/format";

type NavFn = (view: string, opts?: { logQuery?: string; clientId?: string }) => void;

interface ClientUsage {
  requests: number;
  cu: number;
  keys: number;
}

export function Clients({
  focusClientId,
  onNavigate,
}: {
  focusClientId?: string | null;
  onNavigate: NavFn;
}): JSX.Element {
  const clients = useAsync(() => api.listClients(), []);
  const attribution = useAsync(() => api.usageAttribution("week"), []);
  const [selected, setSelected] = useState<string | null>(focusClientId ?? null);

  // The URL (#/clients/<id>) is the source of truth for the open client.
  useEffect(() => {
    setSelected(focusClientId ?? null);
  }, [focusClientId]);

  const usageByClient = useMemo(() => {
    const map = new Map<string, ClientUsage>();
    for (const k of attribution.data?.keys ?? []) {
      if (!k.client_id) continue;
      const u = map.get(k.client_id) ?? { requests: 0, cu: 0, keys: 0 };
      u.requests += k.requests;
      u.cu += k.cu;
      u.keys += 1;
      map.set(k.client_id, u);
    }
    return map;
  }, [attribution.data]);

  const rows = clients.data?.clients ?? [];
  const selectedClient = rows.find((c) => c.id === selected) ?? null;

  return (
    <>
      <div className="topbar">
        <div className="row">
          <h1>Clients</h1>
          <WiredTo id={["clients.list", "monitoring.attribution"]} label="Clients" />
        </div>
        <div className="row">
          <button
            className="btn"
            onClick={() => { clients.reload(); attribution.reload(); }}
            data-wiring="clients.list monitoring.attribution"
          >
            <Icon name="refresh" size={16} /> Refresh
          </button>
          <WiredTo id={["clients.list", "monitoring.attribution"]} label="Refresh" />
          <button className="btn" onClick={() => onNavigate("monitoring")} data-wiring="local.navigation">
            <Icon name="activity" size={16} /> Monitoring
          </button>
          <WiredTo id="local.navigation" label="Monitoring link" />
        </div>
      </div>

      <div className="grid">
        <div className="card col-12" data-wiring="clients.list">
          <AsyncBoundary
            status={clients.status}
            error={clients.error}
            isEmpty={rows.length === 0}
            emptyText="No clients yet."
            onRetry={clients.reload}
          >
            <div className="scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>
                      <span className="row" style={{ gap: 8 }}>
                        Client
                        <WiredTo id="local.select-client" label="Select a client" />
                      </span>
                    </th>
                    <th>Email</th>
                    <th>Billing ref</th>
                    <th>Status</th>
                    <th style={{ textAlign: "right" }}>Keys</th>
                    <th style={{ textAlign: "right" }}>Requests (wk)</th>
                    <th style={{ textAlign: "right" }}>CU (wk)</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => {
                    const u = usageByClient.get(c.id);
                    return (
                      <tr
                        key={c.id}
                        className={`clickrow ${selected === c.id ? "selected" : ""}`}
                        onClick={() => {
                          const next = c.id === selected ? null : c.id;
                          setSelected(next);
                          onNavigate("clients", next ? { clientId: next } : {});
                        }}
                        aria-selected={selected === c.id}
                        data-wiring="local.select-client"
                      >
                        <td className="mono">{c.id.slice(0, 12)}</td>
                        <td>{c.email ?? <span className="muted">—</span>}</td>
                        <td className="mono muted">{c.external_ref ?? "—"}</td>
                        <td>
                          <Badge tone={statusTone(c.status)}>{c.status}</Badge>
                        </td>
                        <td className="tabular" style={{ textAlign: "right" }}>{u ? num(u.keys) : "0"}</td>
                        <td className="tabular" style={{ textAlign: "right" }}>{u ? num(u.requests) : "0"}</td>
                        <td className="tabular" style={{ textAlign: "right" }}>{u ? num(Math.round(u.cu)) : "0"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </AsyncBoundary>
        </div>

        {selectedClient && (
          <ClientDetail
            key={selectedClient.id}
            clientId={selectedClient.id}
            attributionKeys={attribution.data?.keys ?? []}
            onClose={() => {
              setSelected(null);
              onNavigate("clients");
            }}
          />
        )}
      </div>
    </>
  );
}

function ClientDetail({
  clientId,
  attributionKeys,
  onClose,
}: {
  clientId: string;
  attributionKeys: KeyAttribution[];
  onClose: () => void;
}): JSX.Element {
  const endpoints = useAsync(() => api.listWebhookEndpoints(clientId), [clientId]);
  const endpointIds = (endpoints.data?.endpoints ?? []).map((e) => e.id);
  const endpointKey = endpointIds.join(",");
  // Deliveries are requested per endpoint (filtered on the server), so a busy
  // engine's other clients can never push this client's deliveries off the page.
  const deliveries = useAsync(async () => {
    const pages = await Promise.all(
      endpointIds.map((id) => api.listDeliveries({ endpoint_id: id, limit: 100 })),
    );
    return pages
      .flatMap((p) => p.deliveries)
      .sort((a, b) => b.created_at - a.created_at);
  }, [endpointKey]);

  const clientKeys = attributionKeys.filter((k) => k.client_id === clientId);
  const clientDeliveries = deliveries.data ?? [];

  return (
    <div className="card col-12" data-wiring="clients.endpoints clients.deliveries">
      <div className="row spread" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>
          Client <span className="mono">{clientId.slice(0, 12)}</span>
        </h2>
        <div className="row">
          <button
            className="btn"
            onClick={onClose}
            aria-label="Close client detail"
            data-wiring="local.close-detail"
          >
            Close
          </button>
          <WiredTo id="local.close-detail" />
        </div>
      </div>

      <div className="row panel-title">
        <h3 className="detail-h">Keys (usage this week)</h3>
        <WiredTo id="monitoring.attribution" label="Keys (usage this week)" />
      </div>
      {clientKeys.length === 0 ? (
        <div className="empty">No key usage in this window.</div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Key</th>
              <th style={{ textAlign: "right" }}>Requests</th>
              <th style={{ textAlign: "right" }}>CU</th>
              <th style={{ textAlign: "right" }}>Errors</th>
            </tr>
          </thead>
          <tbody>
            {clientKeys.map((k) => (
              <tr key={k.key_id}>
                <td className="mono">
                  {k.key_prefix ?? k.key_id}
                  {k.key_label && <div className="sub">{k.key_label}</div>}
                </td>
                <td className="tabular" style={{ textAlign: "right" }}>{num(k.requests)}</td>
                <td className="tabular" style={{ textAlign: "right" }}>{num(Math.round(k.cu))}</td>
                <td className="tabular" style={{ textAlign: "right" }}>{num(k.errors)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="row panel-title" style={{ marginTop: 20 }}>
        <h3 className="detail-h">Webhook endpoints</h3>
        <WiredTo id="clients.endpoints" />
      </div>
      <AsyncBoundary
        status={endpoints.status}
        error={endpoints.error}
        isEmpty={(endpoints.data?.endpoints ?? []).length === 0}
        emptyText="No webhook endpoints."
        onRetry={endpoints.reload}
      >
        <table className="table">
          <thead>
            <tr>
              <th>URL</th>
              <th>Events</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {(endpoints.data?.endpoints ?? []).map((e) => (
              <tr key={e.id}>
                <td className="mono">{e.url}</td>
                <td className="muted">{e.event_types ? e.event_types.join(", ") : "all"}</td>
                <td>
                  <Badge tone={e.disabled ? "neutral" : "ok"}>{e.disabled ? "disabled" : "enabled"}</Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </AsyncBoundary>

      <div className="row panel-title" style={{ marginTop: 20 }}>
        <h3 className="detail-h">Recent deliveries</h3>
        <WiredTo id="clients.deliveries" />
      </div>
      <AsyncBoundary
        status={endpoints.status === "ready" ? deliveries.status : endpoints.status}
        error={endpoints.status === "error" ? endpoints.error : deliveries.error}
        isEmpty={clientDeliveries.length === 0}
        emptyText="No deliveries."
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
              </tr>
            </thead>
            <tbody>
              {clientDeliveries.map((d) => (
                <tr key={d.id}>
                  <td className="mono">{d.event_type}</td>
                  <td>
                    <Badge tone={statusTone(d.status)}>{d.status}</Badge>
                  </td>
                  <td className="tabular" style={{ textAlign: "right" }}>{d.attempts}</td>
                  <td className="muted">
                    {d.last_status_code ?? (d.last_error ? "error" : "—")}
                  </td>
                  <td className="muted tabular">{clockTime(d.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AsyncBoundary>
    </div>
  );
}
