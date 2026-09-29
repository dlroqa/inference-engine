import { useState, type JSX } from "react";
import { api, ApiError, type PlanRow } from "../lib/api";
import { useAsync } from "../hooks/useAsync";
import { useConfirm } from "../hooks/useConfirm";
import { useAuthFailure } from "../hooks/useAuthScope";
import { AsyncBoundary } from "../components/Panel";
import { Icon } from "../components/Icon";
import { WiredTo } from "../components/WiredTo";
import { num } from "../lib/format";

interface PlanForm {
  id: string;
  name: string;
  quota5h: string;
  quotaWeek: string;
  rate: string;
  models: string;
}

const EMPTY: PlanForm = { id: "", name: "", quota5h: "0", quotaWeek: "0", rate: "", models: "" };

function toForm(p: PlanRow): PlanForm {
  return {
    id: p.id,
    name: p.name,
    quota5h: String(p.quota_5h_cu),
    quotaWeek: String(p.quota_weekly_cu),
    rate: p.rate_limit_per_min === null ? "" : String(p.rate_limit_per_min),
    models: (p.allowed_models ?? []).join(", "),
  };
}

function modelsList(text: string): string[] | null {
  const names = text
    .split(",")
    .map((m) => m.trim())
    .filter(Boolean);
  return names.length > 0 ? names : null;
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
  const existing = rows.find((p) => p.id === form.id.trim()) ?? null;

  const set = (field: keyof PlanForm) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [field]: e.target.value }));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSaved(null);
    const id = form.id.trim();
    if (existing) {
      const ok = await confirm({
        title: `Update plan ${id}?`,
        body: (
          <>
            Every client on <span className="mono">{id}</span> gets the new limits and model list
            immediately.
          </>
        ),
        confirmLabel: "Update plan",
        tone: "primary",
        wiring: "plans.upsert",
      });
      if (!ok) return;
    }
    setSaving(true);
    try {
      const plan = await api.upsertPlan({
        id,
        name: form.name.trim(),
        quota_5h_cu: Number(form.quota5h),
        quota_weekly_cu: Number(form.quotaWeek),
        rate_limit_per_min: form.rate.trim() === "" ? null : Number(form.rate),
        allowed_models: modelsList(form.models),
      });
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
                      {p.rate_limit_per_min === null ? "no limit" : num(p.rate_limit_per_min)}
                    </td>
                    <td className="muted">{p.allowed_models ? p.allowed_models.join(", ") : "all models"}</td>
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
            <div className="sub" id="plan-rate-help">Leave empty for no per-plan rate limit.</div>
          </div>
          <div className="field">
            <label htmlFor="plan-models">Allowed models (optional)</label>
            <input id="plan-models" className="input" value={form.models} onChange={set("models")} aria-describedby="plan-models-help" />
            <div className="sub" id="plan-models-help">Comma-separated model names. Leave empty to allow all models.</div>
          </div>
          <div className="row">
            <button className="btn primary" type="submit" disabled={saving}>
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
