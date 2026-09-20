import type { JSX, ReactNode } from "react";
import type { ConnStatus } from "../hooks/useLiveMetrics";
import type { AsyncStatus } from "../hooks/useAsync";

const CONN_LABEL: Record<ConnStatus, string> = {
  connecting: "Connecting…",
  live: "Live",
  polling: "Polling",
  down: "Disconnected",
};

const CONN_CLASS: Record<ConnStatus, string> = {
  connecting: "",
  live: "live",
  polling: "polling",
  down: "down",
};

export function ConnBadge({ status }: { status: ConnStatus }): JSX.Element {
  return (
    <span className={`conn ${CONN_CLASS[status]}`} role="status" aria-live="polite">
      <span className="dot" />
      {CONN_LABEL[status]}
    </span>
  );
}

// Renders loading / error / empty / content for an async section. Keeps behavior
// consistent across views and gives the required loading/disconnected/error states
// a single, testable implementation.
export function AsyncBoundary({
  status,
  error,
  isEmpty,
  emptyText = "Nothing to show yet.",
  onRetry,
  children,
}: {
  status: AsyncStatus;
  error?: string | null;
  isEmpty?: boolean;
  emptyText?: string;
  onRetry?: () => void;
  children: ReactNode;
}): JSX.Element {
  if (status === "loading") {
    return (
      <div className="empty row" style={{ justifyContent: "center" }}>
        <span className="spinner" aria-label="Loading" role="status" /> Loading…
      </div>
    );
  }
  if (status === "error") {
    return (
      <div className="banner err" role="alert">
        {error ?? "Something went wrong."}
        {onRetry && (
          <>
            {" "}
            <button className="btn" onClick={onRetry} style={{ marginLeft: 8 }}>
              Retry
            </button>
          </>
        )}
      </div>
    );
  }
  if (isEmpty) {
    return <div className="empty">{emptyText}</div>;
  }
  return <>{children}</>;
}
