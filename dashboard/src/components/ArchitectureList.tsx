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

function Observed({ node }: { node: ArchNode }): JSX.Element {
  const { status } = node;
  const sources = node.sources.map((s) => SOURCE_LABELS[s]).join("; ");
  if (node.sources.length === 0) return <>No live source: this is a static description.</>;
  if (status.observedAt === null) return <>Not observed yet. Source: {sources}.</>;
  if (status.stale) {
    return (
      <>
        Last observed {clockTime(status.observedAt)}; the latest read failed or the connection was lost. Source:{" "}
        {sources}.
      </>
    );
  }
  return (
    <>
      Observed {clockTime(status.observedAt)}. Source: {sources}.
    </>
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
      <p className="sub">
        <Observed node={node} />
      </p>
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
