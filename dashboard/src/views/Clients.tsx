import { useEffect, useMemo, useRef, useState, type JSX } from "react";
import { api, type KeyAttribution } from "../lib/api";
import { useAsync } from "../hooks/useAsync";
import { AsyncBoundary } from "../components/Panel";
import { Badge, statusTone } from "../components/widgets";
import { Icon } from "../components/Icon";
import { WiredTo } from "../components/WiredTo";
import { num } from "../lib/format";
import { PlansPanel } from "./Plans";
import {
  ClientKeySection,
  CreateClientCard,
  DeliveriesSection,
  EndpointsSection,
  ReconcileSection,
} from "./ClientAdmin";

type Tab = "clients" | "plans";
const TABS: { id: Tab; label: string }[] = [
  { id: "clients", label: "Clients" },
  { id: "plans", label: "Plans" },
];

type NavFn = (view: string, opts?: { logQuery?: string; clientId?: string; keepFocus?: boolean }) => void;

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
  const [tab, setTab] = useState<Tab>("clients");
  const tabRefs = useRef<Record<Tab, HTMLButtonElement | null>>({ clients: null, plans: null });

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

  // Selecting the open client again closes it. From the row's button, focus
  // stays on that button (the shell does not move it to the page content).
  const toggle = (id: string, keepFocus: boolean) => {
    const next = id === selected ? null : id;
    setSelected(next);
    onNavigate("clients", next ? { clientId: next, keepFocus } : { keepFocus });
  };
  const selectedClient = rows.find((c) => c.id === selected) ?? null;

  // Arrow keys move between the tabs (and select), as a tablist should.
  const onTabKey = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    const i = TABS.findIndex((t) => t.id === tab);
    let next: number | null = null;
    if (e.key === "ArrowRight") next = (i + 1) % TABS.length;
    if (e.key === "ArrowLeft") next = (i - 1 + TABS.length) % TABS.length;
    if (e.key === "Home") next = 0;
    if (e.key === "End") next = TABS.length - 1;
    if (next === null) return;
    e.preventDefault();
    const id = TABS[next].id;
    setTab(id);
    tabRefs.current[id]?.focus();
  };

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

      <div className="row" style={{ marginBottom: 16 }} data-wiring="local.clients-tab">
        <div className="segmented" role="tablist" aria-label="Clients sections">
          {TABS.map((t) => (
            <button
              key={t.id}
              ref={(el) => {
                tabRefs.current[t.id] = el;
              }}
              id={`clients-tab-${t.id}`}
              role="tab"
              type="button"
              className="segbtn"
              aria-selected={tab === t.id}
              aria-controls={`clients-panel-${t.id}`}
              tabIndex={tab === t.id ? 0 : -1}
              onClick={() => setTab(t.id)}
              onKeyDown={onTabKey}
            >
              {t.label}
            </button>
          ))}
        </div>
        <WiredTo id="local.clients-tab" />
      </div>

      {tab === "plans" ? (
        <div role="tabpanel" id="clients-panel-plans" aria-labelledby="clients-tab-plans">
          <PlansPanel />
        </div>
      ) : (
      <div className="grid" role="tabpanel" id="clients-panel-clients" aria-labelledby="clients-tab-clients">
        <CreateClientCard
          onCreated={(client) => {
            clients.reload();
            setSelected(client.id);
            onNavigate("clients", { clientId: client.id, keepFocus: true });
          }}
        />
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
                        onClick={() => toggle(c.id, false)}
                        aria-selected={selected === c.id}
                        data-wiring="local.select-client"
                      >
                        <td>
                          <button
                            type="button"
                            className="rowbtn mono"
                            aria-pressed={selected === c.id}
                            aria-label={`Client ${c.id.slice(0, 12)}${c.email ? ` (${c.email})` : ""}`}
                            data-client-id={c.id}
                            onClick={(e) => {
                              // The row also selects on click; handle it once, here.
                              e.stopPropagation();
                              toggle(c.id, true);
                            }}
                          >
                            {c.id.slice(0, 12)}
                          </button>
                        </td>
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
      )}
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
  const endpointRows = endpoints.data?.endpoints ?? [];
  const endpointKey = endpointRows.map((e) => e.id).join(",");

  const clientKeys = attributionKeys.filter((k) => k.client_id === clientId);

  return (
    <div className="card col-12" data-wiring="clients.endpoints clients.deliveries" data-client-detail={clientId}>
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

      <ClientKeySection clientId={clientId} />
      <ReconcileSection clientId={clientId} />
      <EndpointsSection clientId={clientId} endpoints={endpoints} onChanged={endpoints.reload} />
      {endpoints.status === "ready" && <DeliveriesSection endpoints={endpointRows} endpointKey={endpointKey} />}
    </div>
  );
}
