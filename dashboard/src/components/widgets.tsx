import type { JSX, ReactNode } from "react";

export function StatCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
}): JSX.Element {
  return (
    <div className="card stat">
      <div className="label">{label}</div>
      <div className="value tabular">{value}</div>
      {sub !== undefined && <div className="sub">{sub}</div>}
    </div>
  );
}

export function Meter({
  label,
  percent,
  detail,
}: {
  label: string;
  percent: number | null;
  detail?: string;
}): JSX.Element {
  const value = percent === null ? 0 : Math.max(0, Math.min(100, percent));
  const level = value >= 90 ? "danger" : value >= 75 ? "warn" : "";
  const unavailable = percent === null;
  return (
    <div className="meter">
      <div className="head">
        <span>{label}</span>
        <span className="tabular muted">{unavailable ? "unavailable" : `${value.toFixed(0)}%`}</span>
      </div>
      <div
        className="track"
        role="meter"
        aria-label={label}
        aria-valuenow={unavailable ? undefined : Math.round(value)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className={`fill ${level}`} style={{ width: `${value}%` }} />
      </div>
      {detail && <div className="sub">{detail}</div>}
    </div>
  );
}

export type ToneName = "ok" | "warn" | "danger" | "neutral";

export function Badge({ tone, children }: { tone: ToneName; children: ReactNode }): JSX.Element {
  return <span className={`badge ${tone}`}>{children}</span>;
}

const CATEGORY_TONE: Record<string, ToneName> = {
  validation: "warn",
  auth: "warn",
  limit: "warn",
  model: "warn",
  backend: "danger",
  cancellation: "neutral",
  internal: "danger",
};

export function categoryTone(category: string | null | undefined): ToneName {
  if (!category) return "neutral";
  return CATEGORY_TONE[category] ?? "neutral";
}

export function stateTone(state: string): ToneName {
  if (state === "ready" || state === "generating") return "ok";
  if (state === "failed") return "danger";
  if (state === "loading") return "warn";
  return "neutral";
}
