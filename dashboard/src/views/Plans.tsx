import { useState, type JSX } from "react";
import { api, ApiError, type PlanRow, type PlanUpsert } from "../lib/api";
import { useAsync } from "../hooks/useAsync";
import { useConfirm } from "../hooks/useConfirm";
import { useAuthFailure } from "../hooks/useAuthScope";
import { AsyncBoundary } from "../components/Panel";
import { Icon } from "../components/Icon";
import { WiredTo } from "../components/WiredTo";
import { num } from "../lib/format";

// allowed_models has three distinct values in the engine: null (every model),
// [] (no model) and a non-empty list (only those). The form keeps them apart so
// an edit never turns one into another unless the operator chooses to.
type ModelsMode = "all" | "only" | "none";

interface PlanForm {
  id: string;
  name: string;
  quota5h: string;
  quotaWeek: string;
  rate: string;
  modelsMode: ModelsMode;
  models: string;
}

const EMPTY: PlanForm = {
  id: "",
  name: "",
  quota5h: "0",
  quotaWeek: "0",
  rate: "",
  modelsMode: "all",
  models: "",
};

function toForm(p: PlanRow): PlanForm {
  const mode: ModelsMode = p.allowed_models === null ? "all" : p.allowed_models.length === 0 ? "none" : "only";
  return {
    id: p.id,
    name: p.name,
    quota5h: String(p.quota_5h_cu),
    quotaWeek: String(p.quota_weekly_cu),
    rate: p.rate_limit_per_min === null ? "" : String(p.rate_limit_per_min),
    modelsMode: mode,
    models: (p.allowed_models ?? []).join(", "),
  };
}

function modelNames(text: string): string[] {
  return text
    .split(",")
    .map((m) => m.trim())
    .filter(Boolean);
}

function allowedModels(form: PlanForm): string[] | null {
  if (form.modelsMode === "all") return null;
  if (form.modelsMode === "none") return [];
  return modelNames(form.models);
}

// Plain-language model entitlement, shared by the table and the confirmations.
function describeModels(models: string[] | null): string {
  if (models === null) return "All models";
  if (models.length === 0) return "No models (every request is refused)";
  return models.join(", ");
}

// null inherits the engine-wide rate_limit_per_min; 0 turns the limit off.
function describeRate(rate: number | null): string {
  if (rate === null) return "Engine default";
  if (rate === 0) return "Unlimited";
  return num(rate);
}

// Plans are operator-defined entitlements. A plan is created or replaced by id
// (upsert); there is no delete in the engine's API.
export function PlansPanel(): JSX.Element {
  const plans = useAsync(() => api.listPlans(), []);
  const [form, setForm] = useState<PlanForm>(EMPTY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [confirm, confirmDialog] = useConfirm();
  const reportAuthFailure = useAuthFailure();
  const rows = plans.data?.plans ?? [];
  // Saving is an upsert. Whether it creates or replaces is only knowable from
  // an up-to-date list, so nothing is sent while the list is loading or failed.
  const listReady = plans.status === "ready";
  const existing = rows.find((p) => p.id === form.id.trim()) ?? null;

  const set = (field: Exclude<keyof PlanForm, "modelsMode">) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [field]: e.target.value }));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSaved(null);
    if (!listReady) return;
    const models = allowedModels(form);
    if (form.modelsMode === "only" && models?.length === 0) {
      setError('List at least one model name, or choose "No models".');
      return;
    }
    const id = form.id.trim();
    const payload: PlanUpsert = {
      id,
      name: form.name.trim(),
      quota_5h_cu: Number(form.quota5h),
      quota_weekly_cu: Number(form.quotaWeek),
      rate_limit_per_min: form.rate.trim() === "" ? null : Number(form.rate),
      allowed_models: models,
    };
    const summary = (
      <div className="sub" style={{ marginTop: 8 }}>
        Models: {describeModels(models)}. Requests per minute: {describeRate(payload.rate_limit_per_min)}.
      </div>
    );
    // A plan missing from the loaded list may have been created since (another
    // operator, another tab): the save would then replace it, so creation is
    // confirmed too.
    const ok = await confirm(
      existing
        ? {
            title: `Update plan ${id}?`,
            body: (
              <>
                Every client on <span className="mono">{id}</span> gets the new limits and model list
                immediately.
                {summary}
              </>
            ),
            confirmLabel: "Update plan",
            tone: "primary",
            wiring: "plans.upsert",
          }
        : {
            title: `Create plan ${id}?`,
            body: (
              <>
                No plan <span className="mono">{id}</span> was in the list when it last loaded. If one has
                been created since, it is replaced, and every client on it gets these limits and models
                immediately.
                {summary}
              </>
            ),
            confirmLabel: "Create plan",
            tone: "primary",
            wiring: "plans.upsert",
          },
    );
    if (!ok) return;
    setSaving(true);
    try {
      const plan = await api.upsertPlan(payload);
      setSaved(existing ? `Updated plan ${plan.id}.` : `Created plan ${plan.id}.`);
      setForm(EMPTY);
      plans.reload();
    } catch (err) {
      if (reportAuthFailure(err)) return;
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid">
      <div className="card col-8" data-wiring="plans.list">
        <div className="row spread" style={{ marginBottom: 8 }}>
          <div className="row panel-title">
            <h2 style={{ margin: 0 }}>Plans</h2>
            <WiredTo id="plans.list" />
          </div>
          <div className="row">
            <button className="btn" onClick={plans.reload} data-wiring="plans.list">
              <Icon name="refresh" size={16} /> Refresh
            </button>
            <WiredTo id="plans.list" label="Refresh plans" />
          </div>
        </div>
        <AsyncBoundary
          status={plans.status}
          error={plans.error}
          isEmpty={rows.length === 0}
          emptyText="No plans yet. Create one to assign limits."
          onRetry={plans.reload}
        >
          <div className="scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>Plan</th>
                  <th style={{ textAlign: "right" }}>5-hour CU</th>
                  <th style={{ textAlign: "right" }}>Weekly CU</th>
                  <th style={{ textAlign: "right" }}>Requests/min</th>
                  <th>Models</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.id}>
                    <td>
                      <span className="mono">{p.id}</span>
                      <div className="sub">{p.name}</div>
                    </td>
                    <td className="tabular" style={{ textAlign: "right" }}>{num(p.quota_5h_cu)}</td>
                    <td className="tabular" style={{ textAlign: "right" }}>{num(p.quota_weekly_cu)}</td>
                    <td className="tabular" style={{ textAlign: "right" }}>
                      {describeRate(p.rate_limit_per_min)}
                    </td>
                    <td className={p.allowed_models?.length === 0 ? undefined : "muted"}>
                      {p.allowed_models?.length === 0 ? (
                        <span className="badge warn">{describeModels(p.allowed_models)}</span>
                      ) : (
                        describeModels(p.allowed_models)
                      )}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <button
                        className="btn"
                        type="button"
                        onClick={() => {
                          setForm(toForm(p));
                          setSaved(null);
                          setError(null);
                        }}
                        aria-label={`Edit plan ${p.id}`}
                        data-wiring="local.edit-plan"
                      >
                        Edit
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </AsyncBoundary>
      </div>

      <div className="card col-4" style={{ minWidth: 280 }} data-wiring="plans.upsert">
        <div className="row panel-title">
          <h2>{existing ? "Update plan" : "Create plan"}</h2>
          <WiredTo id="plans.upsert" />
          <WiredTo id="local.edit-plan" label="Edit a plan" />
        </div>
        <form onSubmit={submit}>
          <div className="field">
            <label htmlFor="plan-id">Plan id</label>
            <input id="plan-id" className="input mono" value={form.id} onChange={set("id")} required maxLength={128} />
          </div>
          <div className="field">
            <label htmlFor="plan-name">Name</label>
            <input id="plan-name" className="input" value={form.name} onChange={set("name")} required maxLength={256} />
          </div>
          <div className="field">
            <label htmlFor="plan-5h">5-hour quota (CU)</label>
            <input id="plan-5h" className="input" type="number" min={0} step="any" value={form.quota5h} onChange={set("quota5h")} required />
          </div>
          <div className="field">
            <label htmlFor="plan-week">Weekly quota (CU)</label>
            <input id="plan-week" className="input" type="number" min={0} step="any" value={form.quotaWeek} onChange={set("quotaWeek")} required />
          </div>
          <div className="field">
            <label htmlFor="plan-rate">Requests per minute (optional)</label>
            <input id="plan-rate" className="input" type="number" min={0} step={1} value={form.rate} onChange={set("rate")} aria-describedby="plan-rate-help" />
            <div className="sub" id="plan-rate-help">
              Leave empty to use the engine-wide default (rate_limit_per_min). 0 turns the per-minute limit off
              for this plan.
            </div>
          </div>
          <fieldset className="field plan-models" aria-describedby="plan-models-help">
            <legend>Allowed models</legend>
            {(
              [
                ["all", "All models"],
                ["only", "Only these models"],
                ["none", "No models"],
              ] as const
            ).map(([mode, label]) => (
              <label key={mode} className="row check-row">
                <input
                  type="radio"
                  name="plan-models-mode"
                  value={mode}
                  checked={form.modelsMode === mode}
                  onChange={() => setForm((f) => ({ ...f, modelsMode: mode }))}
                />
                {label}
              </label>
            ))}
            {form.modelsMode === "only" && (
              <div className="field" style={{ marginBottom: 0 }}>
                <label htmlFor="plan-models">Model names</label>
                <input
                  id="plan-models"
                  className="input"
                  value={form.models}
                  onChange={set("models")}
                  required
                  aria-describedby="plan-models-names-help"
                />
                <div className="sub" id="plan-models-names-help">
                  Comma-separated. Requests for any other model are refused.
                </div>
              </div>
            )}
            <div className="sub" id="plan-models-help">
              {form.modelsMode === "none"
                ? "Every inference request from this plan's clients is refused."
                : form.modelsMode === "all"
                  ? "Clients on this plan may use any model the engine serves."
                  : "Only the listed models are allowed."}
            </div>
          </fieldset>
          {!listReady && (
            <div className="banner info" id="plan-list-state" role="status" style={{ marginBottom: 12 }}>
              {plans.status === "loading" ? (
                "Loading the plan list. Saving waits for it, so an existing plan is never replaced unnoticed."
              ) : (
                <>
                  The plan list could not be loaded, so an existing plan with this id cannot be detected.
                  Saving is paused until it loads.{" "}
                  <button className="btn" type="button" onClick={plans.reload} data-wiring="plans.list">
                    Retry loading plans
                  </button>
                </>
              )}
            </div>
          )}
          <div className="row">
            <button
              className="btn primary"
              type="submit"
              disabled={saving || !listReady}
              aria-describedby={listReady ? undefined : "plan-list-state"}
            >
              {saving ? <span className="spinner" /> : <Icon name="check" size={16} />}
              {existing ? "Update plan" : "Create plan"}
            </button>
            {form !== EMPTY && (
              <button className="btn" type="button" onClick={() => setForm(EMPTY)} data-wiring="local.edit-plan">
                Clear
              </button>
            )}
          </div>
        </form>
        {error && (
          <div className="banner err" role="alert" style={{ marginTop: 12 }}>
            {error}
          </div>
        )}
        {saved && (
          <div className="banner info" role="status" style={{ marginTop: 12 }}>
            {saved}
          </div>
        )}
      </div>
      {confirmDialog}
    </div>
  );
}
