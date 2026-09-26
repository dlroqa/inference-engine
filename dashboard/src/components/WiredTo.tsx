import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type JSX,
} from "react";
import { Icon } from "./Icon";
import { SUBSYSTEMS, explain, routeFor, type Explanation, type WiringEntry } from "../lib/wiring";

// "How this works" popover for a control or panel. Everything it shows comes from
// the wiring registry (wiring.ts) and the generated route inventory, so it can
// never describe an endpoint or handler the engine does not have.
//
// Opens on mouse hover, on keyboard focus, and on click/tap; a click or tap pins
// it open. Escape, a click or tap outside, or moving focus away closes it. It
// stays open while the pointer is over the popover itself (WCAG 1.4.13).

const HOVER_OPEN_MS = 120;
const HOVER_CLOSE_MS = 200;
const GAP = 8;
const EDGE = 16;
const MAX_WIDTH = 360;

const GATE_TEXT: Record<string, string> = {
  operator: "Operator key",
  client: "Client key",
  api_key: "Any API key",
  public: "None (public)",
  stripe_signature: "Stripe signature",
};

// Hover applies to a mouse only; touch and pen use tap. (Some environments leave
// pointerType empty for mouse events.)
function isMouse(e: React.PointerEvent): boolean {
  return !e.pointerType || e.pointerType === "mouse";
}

// Routes and dotted handler names wrap only after "/" or ".", never mid-word.
function Breakable({ text }: { text: string }): JSX.Element {
  const parts = text.split(/(?<=[/.])/);
  return (
    <>
      {parts.map((p, i) => (
        <span key={i}>
          {p}
          {i < parts.length - 1 && <wbr />}
        </span>
      ))}
    </>
  );
}

function callText(entry: WiringEntry): string {
  return entry.client.startsWith("useLive") ? `${entry.client}() hook` : `api.${entry.client}()`;
}

function WiredFacts({ entry }: { entry: WiringEntry }): JSX.Element {
  const route = routeFor(entry);
  return (
    <dl className="wired-facts">
      <dt>Route</dt>
      <dd className="mono">
        {entry.endpoint.method} <Breakable text={entry.endpoint.path} />
      </dd>
      <dt>Backend function</dt>
      <dd className="mono">
        {route ? <Breakable text={`${route.module}.${route.handler}`} /> : "not in the route inventory"}
      </dd>
      <dt>Access</dt>
      <dd>{route ? (GATE_TEXT[route.gate] ?? route.gate) : "unknown"}</dd>
      <dt>Dashboard call</dt>
      <dd className="mono">{callText(entry)}</dd>
      <dt>Passes through</dt>
      <dd>
        <ol className="wired-chain" aria-label="Subsystems, in order">
          {entry.chain.map((s) => (
            <li key={s} title={SUBSYSTEMS[s].description}>
              {SUBSYSTEMS[s].label}
            </li>
          ))}
        </ol>
      </dd>
      <dt>Audit log</dt>
      <dd>{entry.audited ? "Recorded" : "Not recorded"}</dd>
      {entry.killSwitch && (
        <>
          <dt>Kill switch</dt>
          <dd className="mono">{entry.killSwitch}</dd>
        </>
      )}
      {entry.sideEffects && (
        <>
          <dt>Side effects</dt>
          <dd>{entry.sideEffects}</dd>
        </>
      )}
      {entry.docs && (
        <>
          <dt>Docs</dt>
          <dd className="mono">
            <Breakable text={entry.docs} />
          </dd>
        </>
      )}
    </dl>
  );
}

function ExplanationBlock({ ex }: { ex: Explanation }): JSX.Element {
  const label = ex.kind === "wired" ? ex.entry.label : ex.control.label;
  const what = ex.kind === "wired" ? ex.entry.what : ex.control.what;
  return (
    <section className="wired-entry">
      <div className="wired-head">
        <strong>{label}</strong>
        {ex.kind === "wired" ? (
          <span className="wired-kind wired">Backend call</span>
        ) : (
          <span className="wired-kind local">No backend call</span>
        )}
      </div>
      <p className="wired-what">{what}</p>
      {ex.kind === "wired" ? (
        <WiredFacts entry={ex.entry} />
      ) : (
        <p className="wired-local-note">Runs in this browser only. No request is sent to the engine.</p>
      )}
    </section>
  );
}

export function WiredTo({ id, label }: { id: string | string[]; label?: string }): JSX.Element {
  const ids = Array.isArray(id) ? id : [id];
  const explanations = ids.map(explain);
  const allLocal = explanations.every((e) => e.kind === "local");
  const first = explanations[0];
  const name = label ?? (first.kind === "wired" ? first.entry.label : first.control.label);
  const ariaLabel = `How this works: ${name}${allLocal ? " (no backend call)" : ""}`;

  const popId = useId();
  const wrap = useRef<HTMLSpanElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const timer = useRef<number | undefined>(undefined);
  const fromPointer = useRef(false);
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [style, setStyle] = useState<CSSProperties>({ visibility: "hidden" });

  const clearTimer = () => window.clearTimeout(timer.current);
  const close = useCallback(() => {
    window.clearTimeout(timer.current);
    setOpen(false);
    setPinned(false);
  }, []);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  // Place the popover in the viewport (fixed, so scroll containers never clip
  // it): below the button, or above when there is no room, clamped to the edges.
  const place = useCallback(() => {
    const t = trigger.current?.getBoundingClientRect();
    const p = pop.current;
    if (!t || !p) return;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const width = Math.min(MAX_WIDTH, vw - EDGE * 2);
    const height = p.offsetHeight;
    const left = Math.max(EDGE, Math.min(t.left, vw - width - EDGE));
    let top = t.bottom + GAP;
    if (top + height > vh - EDGE && t.top - GAP - height >= EDGE) top = t.top - GAP - height;
    setStyle({ top, left, width });
  }, []);

  useLayoutEffect(() => {
    if (!open) {
      setStyle({ visibility: "hidden" });
      return;
    }
    place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    const onDown = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) close();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, close, place]);

  return (
    <span
      ref={wrap}
      className="wired"
      data-wiring-ids={ids.join(" ")}
      onPointerEnter={(e) => {
        if (!isMouse(e)) return;
        clearTimer();
        timer.current = window.setTimeout(() => setOpen(true), HOVER_OPEN_MS);
      }}
      onPointerLeave={(e) => {
        if (!isMouse(e) || pinned) return;
        clearTimer();
        timer.current = window.setTimeout(() => setOpen(false), HOVER_CLOSE_MS);
      }}
      onBlur={(e) => {
        if (!wrap.current?.contains(e.relatedTarget as Node | null)) close();
      }}
    >
      <button
        ref={trigger}
        type="button"
        className={`wired-btn${allLocal ? " local" : ""}`}
        aria-label={ariaLabel}
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        aria-describedby={open ? popId : undefined}
        onPointerDown={() => {
          fromPointer.current = true;
        }}
        onFocus={() => {
          // Keyboard focus opens it; a mouse or touch press is handled by click.
          if (!fromPointer.current) setOpen(true);
          fromPointer.current = false;
        }}
        onClick={() => {
          clearTimer();
          if (open && pinned) {
            close();
          } else {
            setOpen(true);
            setPinned(true);
          }
        }}
      >
        <Icon name="info" size={16} />
      </button>
      {open && (
        <div
          ref={pop}
          id={popId}
          className="wired-pop"
          role="group"
          aria-label={ariaLabel}
          tabIndex={-1}
          style={style}
        >
          {explanations.map((ex) => (
            <ExplanationBlock key={ex.kind === "wired" ? ex.entry.id : ex.control.id} ex={ex} />
          ))}
        </div>
      )}
    </span>
  );
}
