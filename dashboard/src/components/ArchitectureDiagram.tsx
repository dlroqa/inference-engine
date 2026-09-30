import type { JSX } from "react";
import { StatusLine } from "./ArchitectureList";
import { ARCH_EDGES, type ArchNode, type NodeId } from "../lib/architecture";

// Inline-SVG layout of the node model: the request path left to right across
// the middle, side systems above and below. Layout only: positions, edges and
// node buttons. Status comes from the node model; edges are decorative
// (aria-hidden), since every relationship is also stated in text.

// Sized so node text stays at 12px or more where the diagram is shown (the
// .arch-diagram-card breakpoint in styles.css); long summaries are clamped on
// the node and shown in full in the details and the text list.
const W = 150;
const H = 104;
const COLS = [8, 188, 368, 548, 728, 908];
const ROWS = { top: 12, path: 196, bottom: 380 };
export const VIEW_W = COLS[5] + W + 8;
export const VIEW_H = ROWS.bottom + H + 12;

const POS: Record<NodeId, { x: number; y: number }> = {
  edges: { x: COLS[0], y: ROWS.path },
  gateway: { x: COLS[1], y: ROWS.path },
  scheduler: { x: COLS[2], y: ROWS.path },
  router: { x: COLS[3], y: ROWS.path },
  backend_pool: { x: COLS[4], y: ROWS.path },
  backend: { x: COLS[5], y: ROWS.path },
  keystore: { x: COLS[0], y: ROWS.top },
  quota: { x: COLS[1], y: ROWS.top },
  billing: { x: COLS[2], y: ROWS.top },
  client_events: { x: COLS[3], y: ROWS.top },
  webhooks: { x: COLS[4], y: ROWS.top },
  audit: { x: COLS[5], y: ROWS.top },
  store: { x: COLS[0], y: ROWS.bottom },
  log_buffer: { x: COLS[1], y: ROWS.bottom },
  telemetry: { x: COLS[2], y: ROWS.bottom },
  model_registry: { x: COLS[4], y: ROWS.bottom },
  model_service: { x: COLS[5], y: ROWS.bottom },
};

/** Endpoints on the facing sides of two boxes. */
function connector(from: NodeId, to: NodeId): string {
  const a = POS[from];
  const b = POS[to];
  if (a.y === b.y) {
    const [l, r] = a.x < b.x ? [a, b] : [b, a];
    const y = a.y + H / 2;
    const x1 = l === a ? l.x + W : r.x;
    const x2 = l === a ? r.x : l.x + W;
    return `M${x1} ${y} L${x2} ${y}`;
  }
  const down = a.y < b.y;
  const x1 = a.x + W / 2;
  const x2 = b.x + W / 2;
  const y1 = down ? a.y + H : a.y;
  const y2 = down ? b.y : b.y + H;
  return `M${x1} ${y1} L${x2} ${y2}`;
}

export function ArchitectureDiagram({
  nodes,
  selected,
  onSelect,
}: {
  nodes: ArchNode[];
  selected: NodeId | null;
  onSelect: (id: NodeId) => void;
}): JSX.Element {
  return (
    <div className="arch-diagram" role="group" aria-label="Architecture diagram">
      <svg viewBox={`0 0 ${VIEW_W} ${VIEW_H}`} className="arch-svg" focusable="false">
        <defs>
          <marker id="arch-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0 L10 5 L0 10 z" className="arch-arrowhead" />
          </marker>
        </defs>
        <g aria-hidden="true">
          {ARCH_EDGES.map((e) => (
            <path
              key={`${e.from}-${e.to}`}
              d={connector(e.from, e.to)}
              className={`arch-edge ${e.kind}`}
              markerEnd={e.kind === "path" ? "url(#arch-arrow)" : undefined}
            />
          ))}
        </g>
        {nodes.map((n) => {
          const p = POS[n.id];
          return (
            <foreignObject key={n.id} x={p.x} y={p.y} width={W} height={H}>
              <button
                type="button"
                className={`arch-node lane-${n.lane}`}
                aria-pressed={selected === n.id}
                onClick={() => onSelect(n.id)}
                data-node={n.id}
                data-wiring="local.architecture-node"
              >
                <span className="arch-node-label">{n.label}</span>
                <StatusLine status={n.status} />
              </button>
            </foreignObject>
          );
        })}
      </svg>
    </div>
  );
}
