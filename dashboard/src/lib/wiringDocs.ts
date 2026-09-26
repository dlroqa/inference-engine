// The UI → subsystem → endpoint table in docs/dashboard.md, rendered from the
// wiring registry (wording) and the generated route inventory (access gates).
//
// Pure functions only: no file or network access. The drift test
// (__tests__/wiringDocs.test.ts) compares the committed block with this output
// and, only when explicitly asked (UPDATE_WIRING_DOCS=1, run in GitHub
// Actions), rewrites it.

import { SWITCH_LABELS } from "./switches";
import { LOCAL_CONTROLS, SUBSYSTEMS, WIRING, routeFor, type DocRef } from "./wiring";

export const START_MARKER = "<!-- wiring-table:start -->";
export const END_MARKER = "<!-- wiring-table:end -->";

/** Text safe inside a Markdown table cell: no row breaks, no column breaks. */
export function cell(text: string): string {
  return text.replace(/\r?\n|\r/g, " ").replace(/\|/g, "\\|").trim();
}

/** A docs reference as a link that resolves from docs/dashboard.md. */
export function docsLink(doc: DocRef): string {
  const target = doc.ref.startsWith("docs/") ? doc.ref.slice("docs/".length) : `../${doc.ref}`;
  return `[${cell(doc.title)}](${target})`;
}

function byId<T extends { id: string }>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

function row(cells: string[]): string {
  return `| ${cells.join(" | ")} |`;
}

/** The generated block (without markers), ending in a newline. */
export function renderWiringTable(): string {
  const lines: string[] = [
    "_Generated from `dashboard/src/lib/wiring.ts` and `routes.generated.json`; do not edit by hand._",
    "",
    "**Controls that call the engine**",
    "",
    row(["Control", "ID", "Endpoint", "Passes through", "Access", "Audited", "Kill switches", "Docs"]),
    row(["---", "---", "---", "---", "---", "---", "---", "---"]),
  ];
  for (const w of byId(WIRING)) {
    const gate = routeFor(w)?.gate;
    const switches = (w.requiredSwitches ?? []).map((s) => `${SWITCH_LABELS[s]} (\`${s}\`)`);
    lines.push(
      row([
        cell(w.label),
        `\`${w.id}\``,
        `\`${w.endpoint.method} ${w.endpoint.path}\``,
        cell(w.chain.map((s) => SUBSYSTEMS[s].label).join(" → ")),
        gate ? `\`${gate}\`` : "—",
        w.audited ? "yes" : "no",
        switches.length > 0 ? cell(switches.join("; ")) : "—",
        w.docs ? docsLink(w.docs) : "—",
      ]),
    );
  }
  lines.push(
    "",
    "**Controls with no backend call** (they change only what this browser shows or stores)",
    "",
    row(["Control", "ID", "What it does", "Afterwards"]),
    row(["---", "---", "---", "---"]),
  );
  for (const c of byId(LOCAL_CONTROLS)) {
    lines.push(row([cell(c.label), `\`${c.id}\``, cell(c.what), c.followUp ? cell(c.followUp) : "—"]));
  }
  return `${lines.join("\n")}\n`;
}

const ANY_MARKER = /<!--\s*wiring-table:[^\n]*/g;

/**
 * Replaces the block between the markers, keeping everything around it.
 * Requires exactly one start marker, then exactly one end marker, each exactly
 * as written above; anything else (missing, duplicate, reversed or malformed)
 * throws rather than guessing.
 */
export function replaceBlock(doc: string, block: string): string {
  const found = doc.match(ANY_MARKER) ?? [];
  const bad = found.filter((m) => m.trim() !== START_MARKER && m.trim() !== END_MARKER);
  if (bad.length > 0) throw new Error(`malformed wiring-table marker: ${bad[0]}`);
  const starts = found.filter((m) => m.trim() === START_MARKER).length;
  const ends = found.filter((m) => m.trim() === END_MARKER).length;
  if (starts !== 1 || ends !== 1) {
    throw new Error(`expected one start and one end wiring-table marker, found ${starts} and ${ends}`);
  }
  const start = doc.indexOf(START_MARKER);
  const end = doc.indexOf(END_MARKER);
  if (end < start) throw new Error("the wiring-table end marker comes before the start marker");
  return `${doc.slice(0, start + START_MARKER.length)}\n${block}${doc.slice(end)}`;
}

/** The block currently between the markers (validated like replaceBlock). */
export function currentBlock(doc: string): string {
  replaceBlock(doc, "");
  return doc.slice(doc.indexOf(START_MARKER) + START_MARKER.length + 1, doc.indexOf(END_MARKER));
}
