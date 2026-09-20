import { useCallback, useEffect, useState, type JSX } from "react";
import { api, ApiError, setApiKey } from "./lib/api";
import { Icon, type IconName } from "./components/Icon";
import { Overview } from "./views/Overview";
import { Models } from "./views/Models";
import { Logs } from "./views/Logs";
import { Keys } from "./views/Keys";

type ViewId = "overview" | "models" | "logs" | "keys";

const NAV: { id: ViewId; label: string; icon: IconName }[] = [
  { id: "overview", label: "Overview", icon: "gauge" },
  { id: "models", label: "Models", icon: "cpu" },
  { id: "logs", label: "Logs", icon: "list" },
  { id: "keys", label: "API keys", icon: "key" },
];

type GateState = "checking" | "ok" | "need-key" | "error";

function ApiKeyGate({ onSubmit, error }: { onSubmit: (key: string) => void; error?: string }): JSX.Element {
  const [value, setValue] = useState("");
  return (
    <div className="main" style={{ maxWidth: 420, margin: "10vh auto" }}>
      <div className="brand" style={{ padding: 0, marginBottom: 24 }}>
        <span className="dot" /> Inference Engine
      </div>
      <div className="card">
        <h2>Operator access</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          This engine requires an API key. Paste an operator key to continue.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (value.trim()) onSubmit(value.trim());
          }}
        >
          <div className="field">
            <label htmlFor="key">API key</label>
            <input
              id="key"
              className="input"
              type="password"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="sk-ie-…"
              autoComplete="off"
            />
          </div>
          {error && (
            <div className="banner err" role="alert">
              {error}
            </div>
          )}
          <button className="btn primary" type="submit" disabled={!value.trim()}>
            Continue
          </button>
        </form>
      </div>
    </div>
  );
}

export function App(): JSX.Element {
  const [view, setView] = useState<ViewId>("overview");
  const [gate, setGate] = useState<GateState>("checking");
  const [gateError, setGateError] = useState<string | undefined>();

  const check = useCallback(() => {
    setGate("checking");
    api
      .overview()
      .then(() => setGate("ok"))
      .catch((e: ApiError) => {
        if (e.status === 401) {
          setGate("need-key");
        } else {
          setGateError(e.message);
          setGate("error");
        }
      });
  }, []);

  useEffect(() => check(), [check]);

  if (gate === "checking") {
    return (
      <div className="empty row" style={{ justifyContent: "center", minHeight: "100dvh" }}>
        <span className="spinner" role="status" aria-label="Loading" /> Connecting…
      </div>
    );
  }

  if (gate === "need-key" || gate === "error") {
    return (
      <ApiKeyGate
        error={gate === "error" ? gateError : undefined}
        onSubmit={(key) => {
          setApiKey(key);
          check();
        }}
      />
    );
  }

  return (
    <div className="app">
      <nav className="sidebar" aria-label="Primary">
        <div className="brand">
          <span className="dot" /> Inference Engine
        </div>
        {NAV.map((item) => (
          <button
            key={item.id}
            className="navbtn"
            aria-current={view === item.id ? "page" : undefined}
            onClick={() => setView(item.id)}
          >
            <Icon name={item.icon} />
            {item.label}
          </button>
        ))}
      </nav>
      <main className="main">
        {view === "overview" && <Overview />}
        {view === "models" && <Models />}
        {view === "logs" && <Logs />}
        {view === "keys" && <Keys />}
      </main>
    </div>
  );
}
