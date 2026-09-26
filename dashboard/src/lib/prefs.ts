// Per-browser dashboard preferences. Storage can be unavailable or throw
// (private mode, blocked site data), so every read and write is guarded and the
// dashboard falls back to the default.

const SHOW_WIRING_KEY = "ie.dashboard.showWiring";

/** Whether inline endpoint chips are shown. Off unless explicitly turned on. */
export function getShowWiring(): boolean {
  try {
    return localStorage.getItem(SHOW_WIRING_KEY) === "1";
  } catch {
    return false;
  }
}

export function setShowWiring(on: boolean): void {
  try {
    if (on) localStorage.setItem(SHOW_WIRING_KEY, "1");
    else localStorage.removeItem(SHOW_WIRING_KEY);
  } catch {
    /* storage unavailable — the choice lasts for this page only */
  }
}
