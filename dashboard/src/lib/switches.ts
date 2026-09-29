// Feature switches as the dashboard explains them. Switch state comes from
// GET /admin/system and is read-only here: configuration is changed on the
// engine, never from the UI. The backend enforces every switch; disabled
// controls only explain what the engine would refuse.

import { ApiError, type SwitchName } from "./api";

/** Human names for every switch in the /admin/system contract. */
export const SWITCH_LABELS: Record<SwitchName, string> = {
  allow_model_management: "Model management",
  allow_network_downloads: "Network model downloads",
  allow_structured_output: "Structured output",
  diagnostics_enabled: "The diagnostics bundle",
  require_auth: "Required authentication",
  webhooks_enabled: "Webhooks",
  client_events_enabled: "Client events",
  ip_allowlist_set: "The IP allowlist",
  grpc_enabled: "gRPC",
};

export const SWITCH_NAMES = Object.keys(SWITCH_LABELS) as SwitchName[];

/**
 * Feature-specific 403 codes and the switch each one names. Only these codes
 * (with status 403) mean "disabled by a switch"; any other 403, including
 * operator_role_required, is an authorization failure.
 */
export const FEATURE_ERROR_SWITCH: Readonly<Record<string, SwitchName>> = {
  model_management_disabled: "allow_model_management",
  downloads_disabled: "allow_network_downloads",
  diagnostics_disabled: "diagnostics_enabled",
};

/** The switch a failed request names, or null when it is not a feature denial. */
export function featureSwitchFor(e: unknown): SwitchName | null {
  if (!(e instanceof ApiError) || e.status !== 403 || !e.code) return null;
  return Object.prototype.hasOwnProperty.call(FEATURE_ERROR_SWITCH, e.code) ? FEATURE_ERROR_SWITCH[e.code] : null;
}

function joinLabels(labels: string[]): string {
  if (labels.length <= 1) return labels.join("");
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`;
}

/**
 * Why an action is unavailable, naming every switch that is off, e.g.
 * "Model management is disabled (allow_model_management=false)."
 */
export function switchReason(off: readonly SwitchName[]): string {
  if (off.length === 0) return "";
  const labels = off.map((s, i) => (i === 0 ? SWITCH_LABELS[s] : lowerFirst(SWITCH_LABELS[s])));
  const verb = off.length === 1 ? "is" : "are";
  const settings = off.map((s) => `${s}=false`).join(", ");
  return `${joinLabels(labels)} ${verb} disabled (${settings}).`;
}

function lowerFirst(s: string): string {
  // Keep acronyms (gRPC, IP) as they are.
  return /^[A-Z][a-z]/.test(s) ? s[0].toLowerCase() + s.slice(1) : s;
}

/**
 * The message for a failed operator action. A feature denial names its switch;
 * an authorization 403 is reported as such; anything else keeps the server's
 * message.
 */
export function describeActionError(e: unknown): string {
  const sw = featureSwitchFor(e);
  if (sw) return `${switchReason([sw])} The engine refused this action.`;
  if (e instanceof ApiError) {
    if (e.status === 403 && e.code === "operator_role_required") {
      return "Operator access required: this key cannot use operator endpoints.";
    }
    return e.message;
  }
  return String(e);
}

/**
 * What each switch controls and where it is set, for the System view's
 * read-only table. `setting` is the config.toml key; the environment variable is
 * IE_ plus the upper-cased key. The engine reads both at startup.
 */
export const SWITCH_DETAILS: Record<SwitchName, { effect: string; setting: string }> = {
  allow_model_management: {
    effect: "Model import, download, load, unload and delete. Off: those return 403 model_management_disabled.",
    setting: "allow_model_management",
  },
  allow_network_downloads: {
    effect: "Model downloads from Hugging Face or a URL. Off: downloads return 403 downloads_disabled.",
    setting: "allow_network_downloads",
  },
  allow_structured_output: {
    effect: "JSON- and grammar-constrained generation. Off: such requests return 400 structured_output_disabled.",
    setting: "allow_structured_output",
  },
  diagnostics_enabled: {
    effect: "The GET /diagnostics support bundle. Off: it returns 403 diagnostics_disabled.",
    setting: "diagnostics_enabled",
  },
  require_auth: {
    effect:
      "Whether inference requests need an API key. This is the effective value: when the setting is not given, a key is required unless the engine binds only to loopback.",
    setting: "require_auth",
  },
  webhooks_enabled: {
    effect: "Signed outbound webhook delivery to client endpoints, with retries.",
    setting: "webhooks_enabled",
  },
  client_events_enabled: {
    effect: "The per-client account event stream (SSE) for client keys.",
    setting: "client_events_enabled",
  },
  ip_allowlist_set: {
    effect:
      "Whether an IP allowlist is configured. When it is, only the listed networks may reach the engine (others get 403). The addresses are not shown here.",
    setting: "ip_allowlist",
  },
  grpc_enabled: {
    effect: "The gRPC inference service, on the port shown under Metadata.",
    setting: "grpc_enabled",
  },
};

export function envVarFor(setting: string): string {
  return `IE_${setting.toUpperCase()}`;
}
