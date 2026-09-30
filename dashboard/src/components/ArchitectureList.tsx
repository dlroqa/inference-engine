import type { JSX } from "react";
import { Icon, type IconName } from "./Icon";
import { buildHash } from "../hooks/useHashRoute";
import { clockTime } from "../lib/format";
import {
  DESTINATION_LABELS,
  SOURCE_LABELS,
  TONE_LABELS,
  type ArchNode,
  type NodeStatus,
  type Provenance,
  type Tone,
} from "../lib/architecture";

// Rendering shared by the diagram, the selected-node details and the text list.
// Everything shown comes from the node model (lib/architecture.ts); nothing
// here fetches or derives status.

const TONE_ICONS: Record<Tone, IconName> = {
  ok: "check",
  attention: "alert",
  off: "stop",
  unknown: "help",
  info: "info",
};

/** Status as an icon plus text (never colour alone). */
export function StatusLine({ status }: { status: NodeStatus }): JSX.Element {
  return (
    <span className={`arch-status tone-${status.tone}`}>
      <Icon name={TONE_ICONS[status.tone]} size={15} />
      <span>
        <span className="sr-only">{TONE_LABELS[status.tone]}: </span>
        {status.summary}
        {status.stale && <span className="arch-stale"> (stale)</span>}
      </span>
    </span>
  );
}

function provenanceText(p: Provenance): string {
  const label = SOURCE_LABELS[p.source];
  switch (p.state) {
    case "current":
      return `${label}: observed ${clockTime(p.at)}`;
    case "stale":
      return `${label}: last observed ${clockTime(p.at)}; the latest read failed or the connection was lost`;
    case "loading":
      return `${label}: loading`;
    case "failed":
      return `${label}: not observed (the read failed)`;
  }
}

/** Where each value came from and when, one line per source (never one shared time). */
function Observed({ node }: { node: ArchNode }): JSX.Element {
  if (node.status.provenance.length === 0) {
    return <p className="sub">No live source: this is a static description.</p>;
  }
  return (
    <ul className="arch-provenance" aria-label={`Sources for ${node.label}`}>
      {node.status.provenance.map((p) => (
        <li key={p.source} className={p.state === "stale" ? "arch-stale" : undefined}>
          {provenanceText(p)}
        </li>
      ))}
    </ul>
  );
}

/** A node's details: status, facts, freshness, relationships and where its controls are. */
export function NodeFacts({ node }: { node: ArchNode }): JSX.Element {
  return (
    <div className="arch-facts">
      <p className="muted arch-desc">{node.description}</p>
      <StatusLine status={node.status} />
      {node.status.facts.length > 0 && (
        <ul className="arch-fact-list">
          {node.status.facts.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      )}
      <Observed node={node} />
      <p className="sub">{node.relation}</p>
      <p className="sub">
        Implemented in <span className="mono">{node.modules.join(", ")}</span>
      </p>
      {node.destination ? (
        <a className="btn arch-open" href={buildHash(node.destination)} data-wiring="local.architecture-open">
          Open {DESTINATION_LABELS[node.destination]}
          <span className="sr-only"> for {node.label}</span>
        </a>
      ) : (
        <p className="sub">{node.noDestinationReason}</p>
      )}
    </div>
  );
}

/** The whole architecture as text: the phone-width presentation and the diagram's alternative. */
export function ArchitectureList({ nodes }: { nodes: ArchNode[] }): JSX.Element {
  const path = nodes.filter((n) => n.lane === "path");
  const side = nodes.filter((n) => n.lane === "side");
  const section = (title: string, list: ArchNode[], ordered: boolean) => {
    const items = list.map((n) => (
      <li key={n.id} className="arch-list-item">
        <h4>{n.label}</h4>
        <NodeFacts node={n} />
      </li>
    ));
    return (
      <section aria-label={title}>
        <h3>{title}</h3>
        {ordered ? <ol className="arch-list">{items}</ol> : <ul className="arch-list">{items}</ul>}
      </section>
    );
  };
  return (
    <div className="arch-text">
      {section("Request path, in order", path, true)}
      {section("Side systems", side, false)}
    </div>
  );
}
