// Typed client for the engine's operator + admin API. All calls are same-origin
// (the SPA is served by the engine); an optional API key is sent as a Bearer
// token so the dashboard also works when the engine is network-bound.

const KEY_STORAGE = "ie.operator.apiKey";

export function getApiKey(): string | null {
  try {
    return localStorage.getItem(KEY_STORAGE);
  } catch {
    return null;
  }
}

export function setApiKey(key: string | null): void {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key);
    else localStorage.removeItem(KEY_STORAGE);
  } catch {
    /* storage unavailable (private mode) — the key simply isn't persisted */
  }
}

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...extra };
  const key = getApiKey();
  if (key) headers["Authorization"] = `Bearer ${key}`;
  return headers;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const base: Record<string, string> = {};
  if (init?.body) base["Content-Type"] = "application/json";
  let resp: Response;
  try {
    resp = await fetch(path, { ...init, headers: authHeaders(base) });
  } catch (e) {
    throw new ApiError(`network error: ${(e as Error).message}`, 0);
  }
  if (!resp.ok) {
    let message = `request failed (${resp.status})`;
    let code: string | undefined;
    try {
      const body = await resp.json();
      message = body?.error?.message ?? message;
      code = body?.error?.code ?? undefined;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(message, resp.status, code);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

// --- Types (mirror the backend contracts) ---

export interface EnergyPanel {
  state: "measured" | "unavailable";
  watts: number | null;
  j_per_token: number | null;
  tokens_per_joule: number | null;
  source: string | null;
  reason?: string | null;
}

export interface MetricsSnapshot {
  ts: number;
  uptime_s: number;
  counters: {
    requests_total: number;
    requests_active: number;
    requests_errors: number;
    prompt_tokens_total: number;
    completion_tokens_total: number;
    requests_per_min: number;
  };
  throughput: { completion_tokens_per_s: number | null };
  resources: {
    available: boolean;
    reason?: string | null;
    cpu_percent: number | null;
    memory: { total: number | null; used: number | null; available: number | null; percent: number | null };
    process_rss: number | null;
    disk: { path: string; total: number | null; used: number | null; free: number | null; percent: number | null };
  };
  gpu: { available: boolean; reason?: string };
  energy: EnergyPanel;
  backend: { state: string; model_id: string | null; available: boolean };
  scheduler: SchedulerPanel | null;
}

export interface SchedulerPanel {
  max_concurrency: number;
  max_queue_depth: number;
  queue_timeout_s: number;
  in_use: number;
  available: number;
  queue_depth: number;
  peak_in_use: number;
  peak_queue_depth: number;
  admitted_total: number;
  rejected_total: number;
  rejected_queue_full: number;
  rejected_timeout: number;
  cancelled_total: number;
  slow_consumer_total: number;
  wait_ms_avg: number | null;
  wait_ms_max: number;
  wait_ms_last: number;
}

export interface ModelPanel {
  configured_id: string;
  configured: boolean;
  state: string;
  loaded: boolean;
  model_id: string | null;
}

export interface Overview {
  version: string;
  ready: boolean;
  checks: Record<string, string>;
  model: ModelPanel;
  metrics: MetricsSnapshot;
}

export interface LogEvent {
  ts: number;
  level: string;
  logger: string | null;
  event: string;
  request_id: string | null;
  route: string | null;
  model: string | null;
  key_id: string | null;
  category: string | null;
  stage: string | null;
  detail: string | null;
  stacktrace: string | null;
}

export type KeyRole = "operator" | "client";

export interface KeyRow {
  id: string;
  prefix: string;
  label: string | null;
  created_at: string;
  last_used_at: string | null;
  revoked: boolean;
  role?: KeyRole;
  client_id?: string | null;
}

// Who the dashboard is acting as (GET /admin/identity). Never contains a token.
export interface Identity {
  kind: "key" | "local";
  auth_required: boolean;
  key: { id: string; prefix: string; label: string | null; role: KeyRole } | null;
}

// Read-only system description (GET /admin/system). Feature switches are
// booleans only; the response never carries secrets, credential URLs or prompts.
export interface SystemSwitches {
  allow_model_management: boolean;
  allow_network_downloads: boolean;
  allow_structured_output: boolean;
  diagnostics_enabled: boolean;
  require_auth: boolean;
  webhooks_enabled: boolean;
  client_events_enabled: boolean;
  ip_allowlist_set: boolean;
  grpc_enabled: boolean;
}

export type SwitchName = keyof SystemSwitches;

export interface SystemInfo {
  build: { version: string; commit: string | null; built_at: string | null };
  readiness: {
    ready: boolean;
    checks: Record<string, string>;
    inference: { available: boolean; model_id?: string | null; state?: string; reason?: string };
  };
  draining: boolean;
  switches: SystemSwitches;
  metadata: { grpc_port: number | null; billing_provider: string | null };
  routing: {
    backend_kind: string;
    virtual_models: { name: string; policy: string }[];
    workload_routing_enabled: boolean;
    workload_rule_count: number;
    remote_workers: string[];
    spillover_providers: string[];
  };
}

export interface CreatedKey {
  id: string;
  prefix: string;
  label: string | null;
  created_at: string;
  token: string;
}

export interface ModelCompat {
  status: "ok" | "needs_backend" | "too_large" | "unknown";
  reason: string;
}

export interface ModelInfo {
  id: string;
  name: string;
  filename: string;
  source_type: "huggingface" | "url" | "import";
  source_ref: string | null;
  sha256: string | null;
  size_bytes: number | null;
  downloaded_bytes: number;
  progress: number | null;
  quant: string | null;
  arch: string | null;
  context_length: number | null;
  status: "downloading" | "verifying" | "ready" | "error" | "cancelled";
  error: string | null;
  active: boolean;
  loaded: boolean;
  added_at: string;
  compat: ModelCompat;
}

export interface DownloadRequest {
  source_type: "huggingface" | "url";
  name?: string | null;
  repo?: string | null;
  filename?: string | null;
  revision?: string;
  url?: string | null;
  expected_sha256?: string | null;
}

export interface FeedEvent {
  type: string;
  ts: number;
  request_id?: string;
  endpoint?: string;
  model?: string | null;
  key_id?: string;
  tokens?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  finish_reason?: string;
  ttft_ms?: number | null;
  total_ms?: number | null;
  category?: string;
  reason?: string;
  retry_after_s?: number;
}

// --- Commercial / monitoring (Block 11) ---

export interface ClientRow {
  id: string;
  external_ref: string | null;
  email: string | null;
  status: string;
}

export interface KeyAttribution {
  key_id: string;
  key_prefix: string | null;
  key_label: string | null;
  client_id: string | null;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  cu: number;
  errors: number;
  last_ts: number | null;
}

export interface AttributionPage {
  window: string;
  since: number;
  keys: KeyAttribution[];
}

export interface TaxonomyPage {
  window: string;
  since: number;
  categories: Record<string, number>;
  total: number;
}

export interface Alert {
  severity: "info" | "warning" | "critical";
  kind: string;
  message: string;
  target_type: string | null;
  target_id: string | null;
}

export interface WebhookEndpointRow {
  id: string;
  client_id: string;
  url: string;
  description: string | null;
  disabled: boolean;
  event_types: string[] | null;
}

export interface DeliveryRow {
  id: string;
  endpoint_id: string;
  event_id: string;
  event_type: string;
  status: string;
  attempts: number;
  next_attempt_at: number;
  last_status_code: number | null;
  last_error: string | null;
  created_at: number;
  updated_at: number;
}

export interface AuditEvent {
  id: number;
  ts: number;
  actor: string | null;
  action: string;
  target: string | null;
  detail: Record<string, unknown> | null;
  hash: string;
}

export interface AuditPage {
  events: AuditEvent[];
  verify?: { ok: boolean; count: number; first_bad_id: number | null };
}

// --- Endpoints ---

export const api = {
  identity: () => request<Identity>("/admin/identity"),
  system: () => request<SystemInfo>("/admin/system"),
  overview: () => request<Overview>("/admin/overview"),
  metrics: () => request<MetricsSnapshot>("/metrics"),
  logs: (params: { limit?: number; level?: string; request_id?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.level) q.set("level", params.level);
    if (params.request_id) q.set("request_id", params.request_id);
    const qs = q.toString();
    return request<{ events: LogEvent[] }>(`/logs${qs ? `?${qs}` : ""}`);
  },
  listKeys: () => request<{ keys: KeyRow[] }>("/admin/keys"),
  createKey: (label: string | null) =>
    request<CreatedKey>("/admin/keys", { method: "POST", body: JSON.stringify({ label }) }),
  revokeKey: (id: string) => request<{ revoked: boolean; id: string }>(`/admin/keys/${id}`, { method: "DELETE" }),
  deleteKey: (id: string) =>
    request<{ deleted: boolean; id: string }>(`/admin/keys/${id}?purge=true`, { method: "DELETE" }),

  // Model lifecycle (Block 6)
  listModels: () => request<{ models: ModelInfo[] }>("/admin/models"),
  importModel: (path: string, name: string | null) =>
    request<ModelInfo>("/admin/models/import", { method: "POST", body: JSON.stringify({ path, name }) }),
  downloadModel: (body: DownloadRequest) =>
    request<ModelInfo>("/admin/models/download", { method: "POST", body: JSON.stringify(body) }),
  cancelDownload: (id: string) =>
    request<{ cancelling: boolean; id: string }>(`/admin/models/${id}/cancel`, { method: "POST" }),
  loadModelById: (id: string) =>
    request<{ result: string; model: ModelInfo }>(`/admin/models/${id}/load`, { method: "POST" }),
  unloadModelById: (id: string) =>
    request<{ result: string; model: ModelInfo }>(`/admin/models/${id}/unload`, { method: "POST" }),
  deleteModel: (id: string) =>
    request<{ deleted: boolean; id: string }>(`/admin/models/${id}`, { method: "DELETE" }),

  // Commercial / monitoring (Block 11)
  listClients: () => request<{ clients: ClientRow[] }>("/admin/billing/clients"),
  usageAttribution: (window: "5h" | "week" = "5h") =>
    request<AttributionPage>(`/admin/usage/attribution?window=${window}`),
  errorTaxonomy: (window: "5h" | "week" = "5h") =>
    request<TaxonomyPage>(`/admin/errors/taxonomy?window=${window}`),
  alerts: () => request<{ alerts: Alert[] }>("/admin/alerts"),
  listWebhookEndpoints: (clientId?: string) =>
    request<{ endpoints: WebhookEndpointRow[] }>(
      `/admin/billing/webhooks/endpoints${clientId ? `?client_id=${encodeURIComponent(clientId)}` : ""}`,
    ),
  listDeliveries: (params: { endpoint_id?: string; status?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.endpoint_id) q.set("endpoint_id", params.endpoint_id);
    if (params.status) q.set("status", params.status);
    if (params.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<{ deliveries: DeliveryRow[] }>(`/admin/billing/webhooks/deliveries${qs ? `?${qs}` : ""}`);
  },
  audit: (params: { limit?: number; verify?: boolean } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.verify) q.set("verify", "true");
    const qs = q.toString();
    return request<AuditPage>(`/admin/audit${qs ? `?${qs}` : ""}`);
  },
};

export function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const key = getApiKey();
  const q = key ? `?api_key=${encodeURIComponent(key)}` : "";
  return `${proto}//${window.location.host}${path}${q}`;
}
