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
import {
  SUBSYSTEMS,
  docsUrl,
  explain,
  routeFor,
  type Explanation,
  type LocalControl,
  type WiringEntry,
} from "../lib/wiring";

// "How this works" popover for a control or panel. Everything it shows comes from
// the wiring registry (wiring.ts) and the generated route inventory, so it can
// never describe an endpoint or handler the engine does not have.
//
// A non-modal disclosure:
// - Opens on mouse hover (and stays open while the pointer is over the panel,
//   WCAG 1.4.13), on keyboard focus, and on click/tap, which pins it open.
// - The panel follows the button in the Tab order and is itself focusable, so
//   keyboard users can scroll it and reach its docs links. It stays open while
//   focus is anywhere in the button/panel region; leaving the region closes it.
// - Escape closes it. From inside the panel, focus returns to the button;
//   otherwise focus stays where it is. Inside a dialog, Escape closes the popover
//   before the dialog, whether it was opened by focus or by hover.
// - A click or tap outside closes it without moving focus.
// - The panel is fixed-positioned and sized to the space the viewport has, with
//   its own scroll region, so it never extends past any viewport edge.

const HOVER_OPEN_MS = 120;
const HOVER_CLOSE_MS = 200;
const GAP = 8;
const EDGE = 16;
const MAX_WIDTH = 360;
const MAX_HEIGHT = 520;
const MIN_USABLE = 160;

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
      {entry.requiredSwitches && entry.requiredSwitches.length > 0 && (
        <SwitchFacts switches={entry.requiredSwitches} />
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
          <dd>
            <a className="wired-doc" href={docsUrl(entry.docs)} target="_blank" rel="noopener noreferrer">
              {entry.docs.title}
              <Icon name="external" size={13} />
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          </dd>
        </>
      )}
    </dl>
  );
}

function SwitchFacts({ switches }: { switches: string[] }): JSX.Element {
  if (switches.length === 1) {
    return (
      <>
        <dt>Kill switch</dt>
        <dd className="mono">{switches[0]}</dd>
      </>
    );
  }
  return (
    <>
      <dt>Kill switches</dt>
      <dd>
        <ul className="wired-switches">
          {switches.map((s) => (
            <li key={s} className="mono">
              {s}
            </li>
          ))}
        </ul>
        <span className="wired-switch-note">
          {switches.length === 2 ? "Both must be on; turning off either" : "All must be on; turning off any"} one
          disables this action.
        </span>
      </dd>
    </>
  );
}

function LocalNote({ control }: { control: LocalControl }): JSX.Element {
  if (!control.followUp) {
    return <p className="wired-local-note">Runs in this browser only. No request is sent to the engine.</p>;
  }
  return (
    <p className="wired-local-note">
      This control itself sends no request. Afterwards: {control.followUp}
    </p>
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
          <span className="wired-kind local">
            {ex.control.followUp ? "No direct engine call" : "No backend call"}
          </span>
        )}
      </div>
      <p className="wired-what">{what}</p>
      {ex.kind === "wired" ? <WiredFacts entry={ex.entry} /> : <LocalNote control={ex.control} />}
    </section>
  );
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(v, hi));
}

export function WiredTo({ id, label }: { id: string | string[]; label?: string }): JSX.Element {
  const ids = Array.isArray(id) ? id : [id];
  const explanations = ids.map(explain);
  const locals = explanations.flatMap((e) => (e.kind === "local" ? [e.control] : []));
  const allLocal = locals.length === explanations.length;
  const first = explanations[0];
  const name = label ?? (first.kind === "wired" ? first.entry.label : first.control.label);
  const suffix = !allLocal
    ? ""
    : locals.some((c) => c.followUp)
      ? " (no direct engine call)"
      : " (no backend call)";
  const ariaLabel = `How this works: ${name}${suffix}`;

  const popId = useId();
  const wrap = useRef<HTMLSpanElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const timer = useRef<number | undefined>(undefined);
  const fromPointer = useRef(false);
  const skipFocusOpen = useRef(false);
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [style, setStyle] = useState<CSSProperties>({ visibility: "hidden" });

  const clearTimer = () => window.clearTimeout(timer.current);
  const close = useCallback(() => {
    window.clearTimeout(timer.current);
    setOpen(false);
    setPinned(false);
  }, []);
  const focusInside = useCallback(
    () => Boolean(wrap.current?.contains(document.activeElement)),
    [],
  );

  useEffect(() => () => window.clearTimeout(timer.current), []);

  // Size and place the panel inside the viewport. The preferred side is below the
  // button; above is used when only it fits. When neither side fits the content,
  // the roomier side is used with a shorter, scrollable panel, and when neither
  // side is usable at all the panel is clamped over the page. The 16 px margin is
  // kept where the viewport allows it.
  const place = useCallback(() => {
    const t = trigger.current?.getBoundingClientRect();
    const p = pop.current;
    if (!t || !p) return;
    const vw = document.documentElement.clientWidth || window.innerWidth;
    const vh = document.documentElement.clientHeight || window.innerHeight;
    const mx = vw >= 200 ? EDGE : 4;
    const my = vh >= 240 ? EDGE : 4;
    const width = Math.max(0, Math.min(MAX_WIDTH, vw - 2 * mx));
    const left = clamp(t.left, mx, Math.max(mx, vw - width - mx));
    const border = p.offsetHeight - p.clientHeight;
    const natural = Math.min(p.scrollHeight + border, MAX_HEIGHT);
    const below = vh - my - (t.bottom + GAP);
    const above = t.top - GAP - my;
    let top: number;
    let maxHeight: number;
    if (natural <= below) {
      top = t.bottom + GAP;
      maxHeight = below;
    } else if (natural <= above) {
      top = t.top - GAP - natural;
      maxHeight = above;
    } else if (Math.max(below, above) >= MIN_USABLE) {
      maxHeight = Math.max(below, above);
      top = below >= above ? t.bottom + GAP : t.top - GAP - maxHeight;
    } else {
      maxHeight = vh - 2 * my;
      top = t.bottom + GAP;
    }
    maxHeight = Math.max(0, Math.min(maxHeight, MAX_HEIGHT, vh - 2 * my));
    top = clamp(top, my, Math.max(my, vh - my - Math.min(natural, maxHeight)));
    setStyle((prev) =>
      prev.top === top && prev.left === left && prev.width === width && prev.maxHeight === maxHeight
        ? prev
        : { top, left, width, maxHeight },
    );
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
    // Escape is taken in the capture phase on `document`, which runs before
    // React's handlers on the app root, including a dialog's Escape-to-close.
    // So an open popover always gets the first Escape, even when focus is
    // elsewhere in the dialog (a hover preview). It only consumes Escape when it
    // is open and not behind the modal holding focus; a closed popover
    // registers nothing, so the dialog's own Escape keeps working.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const target = e.target instanceof Element ? e.target : null;
      const modal = target?.closest('[role="dialog"][aria-modal="true"]');
      if (modal && wrap.current && !modal.contains(wrap.current)) return;
      e.stopPropagation();
      e.preventDefault();
      const fromPanel = Boolean(pop.current?.contains(document.activeElement));
      close();
      if (fromPanel) {
        skipFocusOpen.current = true;
        trigger.current?.focus();
      }
    };
    const onDown = (e: PointerEvent) => {
      if (!(e.target instanceof Node) || !wrap.current?.contains(e.target)) close();
    };
    // The panel's own scrolling must not re-place it (or reset its position).
    const onScroll = (e: Event) => {
      if (e.target instanceof Node && pop.current?.contains(e.target)) return;
      place();
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onDown);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onDown);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", onScroll, true);
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
        // Keyboard focus inside the region keeps it open when the mouse leaves.
        timer.current = window.setTimeout(() => {
          if (!focusInside()) setOpen(false);
        }, HOVER_CLOSE_MS);
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
        onPointerDown={() => {
          fromPointer.current = true;
        }}
        onFocus={() => {
          const skip = skipFocusOpen.current || fromPointer.current;
          skipFocusOpen.current = false;
          fromPointer.current = false;
          // Keyboard focus opens it; a mouse or touch press is handled by click,
          // and focus returned by Escape leaves it closed.
          if (!skip) setOpen(true);
        }}
        onClick={() => {
          clearTimer();
          fromPointer.current = false;
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
          tabIndex={0}
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
