import { describe, expect, it } from "vitest";
import { api } from "../lib/api";
import inventory from "../lib/routes.generated.json";
import { NOT_IN_UI, SUBSYSTEMS, WIRING, routeKey } from "../lib/wiring";

// Drift guards between the UI's wiring explanations, the typed API client, and
// the engine's real route inventory (regenerated and diffed in CI).

const routes = inventory.routes as { method: string; path: string; gate: string }[];
const routeKeys = new Set(routes.map((r) => routeKey(r.method, r.path)));
const wiredKeys = new Set(WIRING.map((w) => routeKey(w.endpoint.method, w.endpoint.path)));

describe("wiring registry", () => {
  it("has unique ids", () => {
    const ids = WIRING.map((w) => w.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("only references endpoints that exist in the engine", () => {
    const missing = WIRING.filter((w) => !routeKeys.has(routeKey(w.endpoint.method, w.endpoint.path)));
    expect(missing.map((w) => `${w.id}: ${w.endpoint.method} ${w.endpoint.path}`)).toEqual([]);
  });

  it("only wires operator-gated endpoints from the operator dashboard", () => {
    const wrongGate = WIRING.filter((w) => {
      const r = routes.find((x) => x.method === w.endpoint.method && x.path === w.endpoint.path);
      return r !== undefined && r.gate !== "operator";
    });
    expect(wrongGate.map((w) => w.id)).toEqual([]);
  });

  it("covers every api.ts client function", () => {
    const used = new Set(WIRING.map((w) => w.client));
    const unexplained = Object.keys(api).filter((fn) => !used.has(fn as never));
    expect(unexplained).toEqual([]);
  });

  it("accounts for every engine route: wired, or listed with a reason", () => {
    const unaccounted = [...routeKeys].filter((k) => !wiredKeys.has(k) && !(k in NOT_IN_UI));
    expect(unaccounted).toEqual([]);
  });

  it("keeps the no-UI list honest (no stale or already-wired entries)", () => {
    const stale = Object.keys(NOT_IN_UI).filter((k) => !routeKeys.has(k));
    const alsoWired = Object.keys(NOT_IN_UI).filter((k) => wiredKeys.has(k));
    expect({ stale, alsoWired }).toEqual({ stale: [], alsoWired: [] });
  });

  it("describes every subsystem it uses", () => {
    for (const w of WIRING) {
      expect(w.chain.length).toBeGreaterThan(0);
      for (const s of w.chain) expect(SUBSYSTEMS[s]).toBeDefined();
    }
  });

  it("never embeds secrets or prompt text in explanations", () => {
    const text = JSON.stringify(WIRING);
    expect(text).not.toMatch(/sk-ie-[A-Za-z0-9_-]{8,}/);
    expect(text).not.toMatch(/whsec_/);
  });
});
