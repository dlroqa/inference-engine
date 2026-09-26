import { useCallback, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from "react";
import { api, ApiError, getApiKey, setApiKey, type Identity } from "./lib/api";
import { Icon, type IconName } from "./components/Icon";
import { Dialog } from "./components/Dialog";
import { WiredTo } from "./components/WiredTo";
import { buildHash, isDrawerEntry, tagDrawerEntry, untaggedState, useHashRoute } from "./hooks/useHashRoute";
import { SystemProvider } from "./hooks/useSystem";
import { AuthScopeProvider, isAuthFailure, type AuthFailureReporter } from "./hooks/useAuthScope";
import { WiringPrefsProvider, useWiringPrefs } from "./hooks/useWiringPrefs";
import { Overview } from "./views/Overview";
import { Monitoring } from "./views/Monitoring";
import { Clients } from "./views/Clients";
import { Models } from "./views/Models";
import { Logs } from "./views/Logs";
import { Security } from "./views/Security";
import { Keys } from "./views/Keys";

type ViewId = "overview" | "monitoring" | "clients" | "models" | "logs" | "security" | "keys";

interface NavOpts {
  logQuery?: string;
  clientId?: string | null;
  /** With `drawer: "open"`, the model whose details open (#/models/<id>). */
  modelId?: string | null;
  /**
   * "open" adds a drawer entry tagged as opened from the list; "close" returns
   * to that list entry (Back), or replaces a direct-linked drawer with the list.
   */
  drawer?: "open" | "close";
  /** Keep focus on the control that navigated (e.g. a row's select button). */
  keepFocus?: boolean;
}

interface NavItem {
  id: ViewId;
  label: string;
  icon: IconName;
}

// Navigation is grouped by what the operator is doing. A section only lists views
// that exist; later slices add their entries alongside the views themselves.
const NAV_SECTIONS: { label: string; items: NavItem[] }[] = [
  {
    label: "Operate",
    items: [
      { id: "overview", label: "Overview", icon: "gauge" },
      { id: "monitoring", label: "Monitoring", icon: "activity" },
      { id: "logs", label: "Logs", icon: "list" },
    ],
  },
  { label: "Serve", items: [{ id: "models", label: "Models", icon: "cpu" }] },
  { label: "Business", items: [{ id: "clients", label: "Clients", icon: "users" }] },
  {
    label: "Access",
    items: [
      { id: "keys", label: "API keys", icon: "key" },
      { id: "security", label: "Security", icon: "shield" },
    ],
  },
];

const VIEW_IDS = new Set<string>(NAV_SECTIONS.flatMap((s) => s.items.map((i) => i.id)));

type GateState =
  | { kind: "checking" }
  | { kind: "ok"; identity: Identity }
  | { kind: "need-key"; rejected: boolean }
  | { kind: "forbidden"; message: string }
  | { kind: "unreachable"; message: string };

function classify(e: unknown): GateState {
  if (e instanceof ApiError) {
    if (e.status === 401) return { kind: "need-key", rejected: getApiKey() !== null };
    if (e.status === 403 && e.code === "operator_role_required") {
      return { kind: "forbidden", message: e.message };
    }
    if (e.status === 0) return { kind: "unreachable", message: "The engine could not be reached." };
    return { kind: "unreachable", message: `${e.message} (HTTP ${e.status})` };
  }
  return { kind: "unreachable", message: String(e) };
}

function KeyForm({
  onSubmit,
  onCancel,
  error,
}: {
  onSubmit: (key: string) => void;
  onCancel?: () => void;
  error?: string;
}): JSX.Element {
  const [value, setValue] = useState("");
  const hint = ["local.saved-key", "app.identity", ...(onCancel ? ["local.dialog-cancel"] : [])];
  return (
    <form
      data-wiring="local.saved-key app.identity"
      onSubmit={(e) => {
        e.preventDefault();
        if (value.trim()) onSubmit(value.trim());
      }}
    >
      <div className="field">
        <label htmlFor="operator-key">Operator API key</label>
        <input
          id="operator-key"
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
      <div className="row">
        <button className="btn primary" type="submit" disabled={!value.trim()}>
          Continue
        </button>
        {onCancel && (
          <button className="btn" type="button" onClick={onCancel} data-wiring="local.dialog-cancel">
            Cancel
          </button>
        )}
        {/* After the buttons, so a dialog's initial focus lands on the key field. */}
        <WiredTo id={hint} label="Operator sign-in" />
      </div>
    </form>
  );
}

function GateCard({ title, children }: { title: string; children: ReactNode }): JSX.Element {
  return (
    <div className="main" style={{ maxWidth: 440, margin: "10vh auto" }}>
      <div className="brand" style={{ padding: 0, marginBottom: 24 }}>
        <span className="dot" /> Inference Engine
      </div>
      <div className="card">
        <h1 className="gate-title">{title}</h1>
        {children}
      </div>
    </div>
  );
}

function IdentityMenu({
  identity,
  onChangeKey,
  onForgetKey,
}: {
  identity: Identity;
  onChangeKey: () => void;
  onForgetKey: () => void;
}): JSX.Element {
  const key = identity.key;
  return (
    <section className="identity" aria-label="Signed-in identity" data-wiring="app.identity local.saved-key">
      <div className="identity-name">
        <Icon name="key" size={16} />
        {key ? (key.label ?? "Unnamed key") : "Local development"}
        <WiredTo id={["app.identity", "local.saved-key"]} />
      </div>
      <div className="sub">
        {key ? (
          <>
            <span className="mono">{key.prefix}…</span> · {key.role}
          </>
        ) : (
          "No key · loopback access while auth is off"
        )}
      </div>
      <div className="row identity-actions">
        <button className="linkbtn" onClick={onChangeKey}>
          Change key
        </button>
        {getApiKey() !== null && (
          <button className="linkbtn" onClick={onForgetKey}>
            Forget key
          </button>
        )}
      </div>
    </section>
  );
}

// Reveals METHOD /path chips beside every "How this works" button. There is no
// app-wide topbar, so the toggle lives in the sidebar brand block.
function ShowWiringToggle(): JSX.Element {
  const { showWiring, setShowWiring } = useWiringPrefs();
  return (
    <div className="row wiring-toggle" data-wiring="local.show-wiring">
      <button
        type="button"
        className="toggle"
        aria-pressed={showWiring}
        onClick={() => setShowWiring(!showWiring)}
      >
        <Icon name="link" size={16} />
        Show wiring
        <span className="toggle-state" aria-hidden="true">
          {showWiring ? "On" : "Off"}
        </span>
      </button>
      <WiredTo id="local.show-wiring" />
    </div>
  );
}

function NotFound({ view, onHome }: { view: string; onHome: () => void }): JSX.Element {
  return (
    <div className="card col-12" data-wiring="local.navigation">
      <h1>Page not available</h1>
      <p className="muted">
        There is no <span className="mono">{view}</span> view in this dashboard.
      </p>
      <button className="btn" onClick={onHome}>
        Go to Overview
      </button>
    </div>
  );
}

export function App(): JSX.Element {
  return (
    <WiringPrefsProvider>
      <Shell />
    </WiringPrefsProvider>
  );
}

function Shell(): JSX.Element {
  const [route, go] = useHashRoute("overview");
  const [gate, setGate] = useState<GateState>({ kind: "checking" });
  const [changingKey, setChangingKey] = useState(false);
  // A new session starts on every identity check and whenever access is lost,
  // so session state (the authenticated views, switch state and its refused-
  // switch overrides) never outlives the key it was fetched with. The ref is the
  // current session for async callbacks; the state remounts the session tree.
  const sessionRef = useRef(0);
  const [session, setSession] = useState(0);
  const main = useRef<HTMLElement>(null);
  const firstRoute = useRef(true);
  const prevRoute = useRef(route);
  const keepFocus = useRef(false);

  const newSession = useCallback(() => {
    sessionRef.current += 1;
    setSession(sessionRef.current);
  }, []);

  const check = useCallback(() => {
    newSession();
    setGate({ kind: "checking" });
    api
      .identity()
      .then((identity) => setGate({ kind: "ok", identity }))
      .catch((e) => setGate(classify(e)));
  }, []);

  useEffect(() => check(), [check]);

  // Auth loss reported by a protected request of session `owner`. Only the first
  // report for the current session acts; later ones (a burst of failing polls)
  // and reports from an earlier session are ignored. The error is classified by
  // the same rules as the identity gate, so no extra request is made and a
  // failing identity check cannot loop.
  const endSession = useCallback(
    (owner: number, e: unknown): boolean => {
      if (!isAuthFailure(e)) return false;
      if (owner !== sessionRef.current) return true;
      newSession();
      setChangingKey(false);
      setGate(classify(e));
      return true;
    },
    [newSession],
  );
  const reportAuthFailure = useMemo<AuthFailureReporter>(
    () => (e) => endSession(session, e),
    [endSession, session],
  );

  // Move focus to the page content after navigation so keyboard and screen-reader
  // users land on the new view (not on first render). Two exceptions keep focus
  // where the operator is: opening or closing the model drawer (the drawer takes
  // focus and returns it itself, including on Back/Forward), and a navigation
  // that asked to keep focus (a client row's select button). Leaving a view,
  // with or without a drawer open, always focuses the destination's content.
  const routeKey = `${route.view}/${route.segments.join("/")}`;
  useEffect(() => {
    const prev = prevRoute.current;
    prevRoute.current = route;
    if (firstRoute.current) {
      firstRoute.current = false;
      return;
    }
    if (keepFocus.current) {
      keepFocus.current = false;
      return;
    }
    if (prev.view === "models" && route.view === "models") return;
    main.current?.focus();
  }, [routeKey]);

  const navigate = useCallback(
    (next: string, opts: NavOpts = {}) => {
      if (next === "models" && opts.drawer === "close") {
        if (isDrawerEntry("models")) window.history.back();
        else go(buildHash("models"), { replace: true, state: untaggedState() });
        return;
      }
      let hash: string;
      if (next === "logs") hash = buildHash("logs", { params: { request_id: opts.logQuery } });
      else if (next === "clients" && opts.clientId) hash = buildHash("clients", { segments: [opts.clientId] });
      else if (next === "models" && opts.drawer === "open" && opts.modelId)
        hash = buildHash("models", { segments: [opts.modelId] });
      else hash = buildHash(next);
      if (window.location.hash === hash) return;
      // Only a navigation that actually happens may consume the keep-focus flag.
      keepFocus.current = Boolean(opts.keepFocus);
      go(hash);
      // Tag only the entry just added for the drawer.
      if (next === "models" && opts.drawer === "open" && opts.modelId) tagDrawerEntry("models");
    },
    [go],
  );

  const submitKey = (key: string) => {
    setApiKey(key);
    setChangingKey(false);
    check();
  };

  const forgetKey = () => {
    setApiKey(null);
    check();
  };

  if (gate.kind === "checking") {
    return (
      <div className="empty row" style={{ justifyContent: "center", minHeight: "100dvh" }}>
        <span className="spinner" role="status" aria-label="Loading" /> Connecting…
      </div>
    );
  }

  if (gate.kind === "need-key") {
    return (
      <GateCard title="Operator access">
        <p className="muted" style={{ marginTop: 0 }}>
          This engine requires an operator API key. Paste one to continue.
        </p>
        <KeyForm
          onSubmit={submitKey}
          error={gate.rejected ? "That key was not accepted (invalid or revoked)." : undefined}
        />
      </GateCard>
    );
  }

  if (gate.kind === "forbidden") {
    return (
      <GateCard title="Operator access required">
        <p className="muted" style={{ marginTop: 0 }}>
          The saved key is valid but belongs to a client. Client keys can call the
          inference API but cannot open the operator dashboard. Use an operator key.
        </p>
        <KeyForm onSubmit={submitKey} />
        <div className="row" style={{ marginTop: 12 }} data-wiring="local.saved-key">
          <button className="linkbtn" onClick={forgetKey}>
            Forget saved key
          </button>
          <WiredTo id="local.saved-key" label="Saved operator key" />
        </div>
      </GateCard>
    );
  }

  if (gate.kind === "unreachable") {
    return (
      <GateCard title="Engine unavailable">
        <div className="banner err" role="alert">
          {gate.message}
        </div>
        <p className="muted">
          This is a connection or server problem, not a key problem. Check that the
          engine is running, then retry.
        </p>
        <div className="row" data-wiring="app.identity">
          <button className="btn primary" onClick={check}>
            <Icon name="refresh" size={16} /> Retry
          </button>
          <WiredTo id="app.identity" label="Engine connection" />
        </div>
      </GateCard>
    );
  }

  const view = route.view;
  const params = route.params;

  return (
    <AuthScopeProvider value={reportAuthFailure}>
      <SystemProvider key={session}>
        <div className="app">
          <nav className="sidebar" aria-label="Primary">
            <div className="brand">
              <span className="dot" /> Inference Engine
              <WiredTo id="local.navigation" label="Navigation" />
            </div>
            <ShowWiringToggle />
            {NAV_SECTIONS.map((section) => (
              <div className="navsection" key={section.label} data-wiring="local.navigation">
                <div className="navsection-label" id={`nav-${section.label}`}>
                  {section.label}
                </div>
                <ul aria-labelledby={`nav-${section.label}`}>
                  {section.items.map((item) => (
                    <li key={item.id}>
                      <a
                        className="navbtn"
                        href={buildHash(item.id)}
                        aria-current={view === item.id ? "page" : undefined}
                      >
                        <Icon name={item.icon} />
                        {item.label}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            <IdentityMenu
              identity={gate.identity}
              onChangeKey={() => setChangingKey(true)}
              onForgetKey={forgetKey}
            />
          </nav>
          <main className="main" ref={main} tabIndex={-1}>
            {view === "overview" && <Overview />}
            {view === "monitoring" && <Monitoring onNavigate={navigate} />}
            {view === "clients" && (
              <Clients focusClientId={route.segments[0] ?? null} onNavigate={navigate} />
            )}
            {view === "models" && <Models modelId={route.segments[0] ?? null} onNavigate={navigate} />}
            {view === "logs" && <Logs requestId={params.get("request_id") ?? undefined} onNavigate={navigate} />}
            {view === "security" && <Security />}
            {view === "keys" && <Keys />}
            {!VIEW_IDS.has(view) && <NotFound view={view} onHome={() => go(buildHash("overview"))} />}
          </main>
          {changingKey && (
            <Dialog title="Change operator key" onClose={() => setChangingKey(false)}>
              <p className="muted" style={{ marginTop: 0 }}>
                The key is stored only in this browser and sent as a Bearer token.
              </p>
              <KeyForm onSubmit={submitKey} onCancel={() => setChangingKey(false)} />
            </Dialog>
          )}
        </div>
      </SystemProvider>
    </AuthScopeProvider>
  );
}
