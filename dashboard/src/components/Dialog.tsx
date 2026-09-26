import { useEffect, useId, useRef, type JSX, type ReactNode } from "react";
import { WiredTo } from "./WiredTo";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Accessible modal: labelled by its title, traps Tab focus, closes on Escape or a
// click on the scrim, and returns focus to whatever opened it.
export function Dialog({
  title,
  onClose,
  children,
  initialFocus,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  initialFocus?: "first" | "panel";
}): JSX.Element {
  const titleId = useId();
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const first = panel.current?.querySelector<HTMLElement>(FOCUSABLE);
    if (initialFocus !== "panel" && first) first.focus();
    else panel.current?.focus();
    return () => opener?.focus?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== "Tab" || !panel.current) return;
    const items = Array.from(panel.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (items.length === 0) {
      e.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      className="scrim"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <h2 id={titleId}>{title}</h2>
        {children}
      </div>
    </div>
  );
}

export interface ConfirmOptions {
  title: string;
  body: ReactNode;
  confirmLabel: string;
  tone?: "danger" | "primary";
  /** Wiring-registry id of the confirmed action; adds a "How this works" hint. */
  wiring?: string;
}

// Confirmation for destructive actions. Cancel is focused first so Enter or a
// stray keypress never confirms by accident; Escape cancels. With `wiring`, a
// hint after the buttons explains the confirmed call and the local Cancel.
export function ConfirmDialog({
  options,
  onResult,
}: {
  options: ConfirmOptions;
  onResult: (confirmed: boolean) => void;
}): JSX.Element {
  return (
    <Dialog title={options.title} onClose={() => onResult(false)}>
      <div className="dialog-body">{options.body}</div>
      <div className="row dialog-actions">
        <button
          className="btn"
          onClick={() => onResult(false)}
          data-wiring={options.wiring ? "local.dialog-cancel" : undefined}
        >
          Cancel
        </button>
        <button
          className={`btn ${options.tone === "primary" ? "primary" : "danger"}`}
          onClick={() => onResult(true)}
          data-wiring={options.wiring}
        >
          {options.confirmLabel}
        </button>
        {options.wiring && (
          <WiredTo id={[options.wiring, "local.dialog-cancel"]} label={options.confirmLabel} />
        )}
      </div>
    </Dialog>
  );
}
