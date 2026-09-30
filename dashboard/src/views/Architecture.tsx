import { useEffect, useMemo, useRef, type JSX } from "react";
import { Icon } from "../components/Icon";
import { WiredTo } from "../components/WiredTo";
import { ArchitectureDiagram } from "../components/ArchitectureDiagram";
import { ArchitectureList, NodeFacts, StatusLine } from "../components/ArchitectureList";
import { useArchitectureData } from "../hooks/useArchitectureData";
import {
  TONE_LABELS,
  deriveNodes,
  resolveFocus,
  type NodeId,
  type Tone,
} from "../lib/architecture";

// The Architecture view: a read-only explanation of the running engine. It
// composes the data hook (requests and their lifecycle), the pure node model
// (status meaning) and the two presentations (diagram, text list). Controls
// stay in their own views; nodes link there.
//
// Focus: `#/architecture?focus=<node>` selects a node. When the address is set
// from outside (a link, Back/Forward, a direct load), focus moves to that
// node's details; selecting a node here updates the address without moving
// focus, and a data refresh never moves focus. An unknown node name shows a
// notice and focuses the page heading.

const LEGEND: Tone[] = ["ok", "attention", "off", "unknown", "info"];

export function Architecture({
  focus,
  onSelect,
}: {
  focus: string | null;
  /** Updates the address (#/architecture?focus=<id>, or none) without a new history entry. */
  onSelect: (id: NodeId | null) => void;
}): JSX.Element {
  const { observations, refresh } = useArchitectureData();
  const nodes = useMemo(() => deriveNodes(observations), [observations]);
  const selected = resolveFocus(focus);
  const unknownFocus = focus !== null && focus !== "" && selected === null;
  const node = selected ? nodes.find((n) => n.id === selected) ?? null : null;

  const heading = useRef<HTMLHeadingElement>(null);
  const details = useRef<HTMLHeadingElement>(null);
  // The focus value this view set itself; its echo in the address must not move focus.
  const own = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    if (own.current !== undefined && own.current === focus) {
      own.current = undefined;
      return;
    }
    own.current = undefined;
    if (selected) details.current?.focus();
    else if (unknownFocus) heading.current?.focus();
    // Only a change of address moves focus; data refreshes do not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus]);

  const select = (id: NodeId | null) => {
    own.current = id;
    onSelect(id);
  };

  // Closing returns focus to the node's diagram button when the diagram is
  // shown, otherwise to the page heading (the details card is removed).
  const closeDetails = () => {
    const id = selected;
    select(null);
    const btn = id ? document.querySelector<HTMLElement>(`.arch-diagram [data-node="${id}"]`) : null;
    if (btn && btn.offsetParent !== null) btn.focus();
    else heading.current?.focus();
  };

  return (
    <div className="grid">
      <div className="card col-12" data-wiring="app.system routing.backends models.list monitoring.alerts overview.metrics">
        <div className="row spread">
          <div className="row panel-title">
            <h1 ref={heading} tabIndex={-1} style={{ margin: 0 }}>
              Architecture
            </h1>
          </div>
          <div className="row">
            <button className="btn" type="button" onClick={refresh}>
              <Icon name="refresh" size={16} /> Refresh
            </button>
            <WiredTo
              id={["app.system", "routing.backends", "models.list", "monitoring.alerts", "overview.metrics"]}
              label="Architecture data"
            />
          </div>
        </div>
        <p className="muted">
          How a request moves through this engine, and the systems beside it. Read-only: each node shows what
          the engine reports now and opens the view with its controls. Configuration is set in{" "}
          <span className="mono">config.toml</span> or <span className="mono">IE_…</span> environment variables.
        </p>
        <ul className="arch-legend" aria-label="Status key">
          {LEGEND.map((t) => (
            <li key={t}>
              <StatusLine status={{ tone: t, summary: TONE_LABELS[t], facts: [], observedAt: null, stale: false }} />
            </li>
          ))}
        </ul>
        <p className="sub">
          Unknown means not observed (loading, a failed read, or nothing reports it), not down. Stale values
          show when they were last observed. Configured adapters are not a hardware qualification.
        </p>
        {unknownFocus && (
          <div className="banner info" role="status">
            There is no <span className="mono">{focus}</span> node. Showing the whole architecture.
          </div>
        )}
      </div>

      <div className="card col-12 arch-diagram-card" data-wiring="local.architecture-node">
        <div className="row panel-title">
          <h2>Request path and side systems</h2>
          <WiredTo id="local.architecture-node" />
        </div>
        <ArchitectureDiagram nodes={nodes} selected={selected} onSelect={(id) => select(selected === id ? null : id)} />
      </div>

      {node && (
        <div className="card col-12" data-wiring="local.architecture-node local.architecture-open">
          <div className="row spread">
            <div className="row panel-title">
              <h2 ref={details} tabIndex={-1}>
                {node.label}
              </h2>
              <WiredTo id={["local.architecture-open", "local.architecture-node"]} label="Node details" />
            </div>
            <button className="btn" type="button" onClick={closeDetails} data-wiring="local.architecture-node">
              Close details
            </button>
          </div>
          <NodeFacts node={node} />
        </div>
      )}

      <div className="card col-12" data-wiring="local.architecture-open">
        <div className="row panel-title">
          <h2>All nodes</h2>
          <WiredTo id="local.architecture-open" />
        </div>
        <ArchitectureList nodes={nodes} />
      </div>
    </div>
  );
}
