import { useState, type JSX, type ReactNode } from "react";
import { api, ApiError } from "../lib/api";
import { useSystem } from "../hooks/useSystem";
import { useAuthFailure } from "../hooks/useAuthScope";
import { AsyncBoundary } from "../components/Panel";
import { Badge } from "../components/widgets";
import { Icon } from "../components/Icon";
import { WiredTo } from "../components/WiredTo";
import { bytes } from "../lib/format";
import {
  SWITCH_DETAILS,
  SWITCH_LABELS,
  SWITCH_NAMES,
  describeActionError,
  envVarFor,
  featureSwitchFor,
  switchReason,
} from "../lib/switches";

// The System view: build, readiness and feature switches from the shared
// GET /admin/system state (read-only: configuration is changed on the engine,
// never here), plus the diagnostics bundle download.

function Row({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

// A check passes with these values; anything else (an error, "pending",
// "draining", "model not ready") is shown as a problem.
function checkTone(value: string): "ok" | "warn" {
  return value === "ok" || value === "applied" ? "ok" : "warn";
}

/** Saves `data` as a JSON file through the browser. Returns the file size. */
function saveJson(data: unknown, filename: string): number {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
  return blob.size;
}

function Diagnostics(): JSX.Element {
  const sys = useSystem();
  const reportAuthFailure = useAuthFailure();
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const off = sys.offSwitches(["diagnostics_enabled"]);

  const download = async () => {
    setBusy(true);
    setDone(null);
    setError(null);
    try {
      const bundle = await api.diagnostics();
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      const size = saveJson(bundle, `inference-engine-diagnostics-${stamp}.json`);
      setDone(`Saved a ${bytes(size)} bundle to your downloads. Its contents are not shown here.`);
    } catch (e) {
      if (reportAuthFailure(e)) return;
      const sw = featureSwitchFor(e);
      if (sw) {
        sys.reportDenied(sw);
        setError(describeActionError(e));
      } else if (e instanceof ApiError && e.status === 403) {
        // Not the diagnostics switch: an authorization failure.
        setError(`Not authorized to download the bundle: ${e.message}`);
      } else {
        setError(`The bundle could not be downloaded: ${describeActionError(e)}`);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card col-6" data-wiring="system.diagnostics">
      <div className="row panel-title">
        <h2>Diagnostics bundle</h2>
        <WiredTo id="system.diagnostics" label="Diagnostics bundle" />
      </div>
      <p className="sub" style={{ marginTop: 0 }}>
        A JSON support file: version, redacted configuration, hardware, metrics and recent
        structured logs (metadata only, never prompts). It is saved by this browser and not shown
        here.
      </p>
      {off.length > 0 && (
        <p className="disabled-reason" id="diagnostics-restriction">
          <Icon name="lock" size={16} />
          <span>
            {switchReason(off)} The engine refuses the download until it is turned on in its
            configuration.
          </span>
        </p>
      )}
      <div className="row">
        <button
          className="btn"
          type="button"
          onClick={download}
          disabled={busy || off.length > 0}
          aria-describedby={off.length > 0 ? "diagnostics-restriction" : undefined}
        >
          {busy ? <span className="spinner" /> : <Icon name="external" size={16} />}
          Download diagnostics bundle
        </button>
        <WiredTo id="system.diagnostics" label="Download diagnostics bundle" />
      </div>
      {done && (
        <p className="sub" role="status" style={{ marginBottom: 0 }}>
          <Icon name="check" size={14} /> {done}
        </p>
      )}
      {error && (
        <div className="banner err" role="alert" style={{ marginTop: 12, marginBottom: 0 }}>
          {error}
        </div>
      )}
    </div>
  );
}

export function System(): JSX.Element {
  const sys = useSystem();
  const info = sys.info;
  const status = sys.status === "ready" ? "ready" : sys.status === "loading" ? "loading" : "error";
  const error =
    sys.status === "absent" ? "System status is unavailable." : `System status could not be loaded (${sys.error}).`;

  return (
    <>
      <div className="topbar">
        <h1>System</h1>
        <div className="row" data-wiring="app.system">
          <button className="btn" type="button" onClick={sys.refresh}>
            <Icon name="refresh" size={16} /> Refresh
          </button>
          <WiredTo id="app.system" label="Refresh" />
        </div>
      </div>

      {info && sys.stale && (
        <div className="banner info" role="status">
          Could not refresh ({sys.error}); showing the last known state.
        </div>
      )}

      <div data-wiring="app.system">
        <AsyncBoundary status={status} error={error} onRetry={sys.refresh}>
          {info && (
            <div className="grid">
              <div className="card col-6" data-wiring="app.system">
                <div className="row panel-title">
                  <h2>Build</h2>
                  <WiredTo id="app.system" label="Build" />
                </div>
                <dl className="kv kv-rows">
                  <Row label="Version">
                    <span className="mono">{info.build.version}</span>
                  </Row>
                  <Row label="Commit">
                    <span className="mono wrap-anywhere">{info.build.commit ?? "not recorded"}</span>
                  </Row>
                  <Row label="Built at">
                    <span className="tabular">{info.build.built_at ?? "not recorded"}</span>
                  </Row>
                </dl>
              </div>

              <div className="card col-6" data-wiring="app.system">
                <div className="row panel-title">
                  <h2>Readiness</h2>
                  <WiredTo id="app.system" label="Readiness" />
                </div>
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  {info.readiness.ready ? (
                    <Badge tone="ok">
                      <Icon name="check" size={14} /> Ready
                    </Badge>
                  ) : (
                    <Badge tone="danger">
                      <Icon name="alert" size={14} /> Not ready
                    </Badge>
                  )}
                  {info.draining && (
                    <Badge tone="warn">
                      <Icon name="stop" size={14} /> Draining for shutdown
                    </Badge>
                  )}
                </div>
                <table className="table" style={{ marginTop: 12 }}>
                  <caption className="sr-only">Readiness checks</caption>
                  <thead>
                    <tr>
                      <th scope="col">Check</th>
                      <th scope="col">Result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(info.readiness.checks).map(([name, value]) => (
                      <tr key={name}>
                        <td>{name}</td>
                        <td>
                          <Badge tone={checkTone(value)}>{value}</Badge>
                        </td>
                      </tr>
                    ))}
                    <tr>
                      <td>inference</td>
                      <td>
                        {info.readiness.inference.available ? (
                          <>
                            <Badge tone="ok">available</Badge>{" "}
                            <span className="mono muted">
                              {info.readiness.inference.model_id ?? ""}
                              {info.readiness.inference.state ? ` · ${info.readiness.inference.state}` : ""}
                            </span>
                          </>
                        ) : (
                          <>
                            <Badge tone="neutral">not available</Badge>{" "}
                            <span className="muted">{info.readiness.inference.reason ?? ""}</span>
                          </>
                        )}
                      </td>
                    </tr>
                  </tbody>
                </table>
                <p className="sub" style={{ marginBottom: 0 }}>
                  Load balancers polling the public <span className="mono">GET /readyz</span> probe get
                  HTTP 200 while the engine is ready and 503 while it is not; the checks are the same.
                  Inference availability is reported separately and only counts toward readiness when
                  the engine is configured to require a loaded model.
                </p>
              </div>

              <div className="card col-12" data-wiring="app.system">
                <div className="row panel-title">
                  <h2>Feature switches</h2>
                  <WiredTo id="app.system" label="Feature switches" />
                </div>
                <p className="sub" style={{ marginTop: 0 }}>
                  Read-only. Each is set in <span className="mono">config.toml</span> or its{" "}
                  <span className="mono">IE_…</span> environment variable, and the engine reads it at
                  startup. The engine enforces every switch itself.
                </p>
                <div className="scroll" role="region" aria-label="Feature switches table" tabIndex={0}>
                  <table className="table">
                    <thead>
                      <tr>
                        <th scope="col">Switch</th>
                        <th scope="col">State</th>
                        <th scope="col">What it controls</th>
                        <th scope="col">Set with</th>
                      </tr>
                    </thead>
                    <tbody>
                      {SWITCH_NAMES.map((name) => {
                        const on = sys.switchState(name);
                        const d = SWITCH_DETAILS[name];
                        return (
                          <tr key={name}>
                            <td>
                              {SWITCH_LABELS[name]}
                              <div className="mono muted">{name}</div>
                            </td>
                            <td>
                              {on === null ? (
                                <Badge tone="neutral">unknown</Badge>
                              ) : (
                                <Badge tone={on ? "ok" : "neutral"}>{on ? "On" : "Off"}</Badge>
                              )}
                            </td>
                            <td className="muted">{d.effect}</td>
                            <td className="mono">
                              {d.setting}
                              <div className="muted">{envVarFor(d.setting)}</div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="card col-6" data-wiring="app.system">
                <div className="row panel-title">
                  <h2>Metadata</h2>
                  <WiredTo id="app.system" label="Metadata" />
                </div>
                <dl className="kv kv-rows">
                  <Row label="gRPC port">
                    <span className="tabular">
                      {info.metadata.grpc_port === null
                        ? "gRPC is off"
                        : info.metadata.grpc_port === 0
                          ? "assigned by the OS at startup (0)"
                          : info.metadata.grpc_port}
                    </span>
                  </Row>
                  <Row label="Billing provider">{info.metadata.billing_provider ?? "none"}</Row>
                </dl>
              </div>

              <Diagnostics />
            </div>
          )}
        </AsyncBoundary>
      </div>
    </>
  );
}
