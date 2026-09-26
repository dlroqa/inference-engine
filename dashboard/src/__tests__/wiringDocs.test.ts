// @vitest-environment node
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  END_MARKER,
  START_MARKER,
  cell,
  currentBlock,
  docsLink,
  renderWiringTable,
  replaceBlock,
} from "../lib/wiringDocs";
import { LOCAL_CONTROLS, WIRING } from "../lib/wiring";

// docs/dashboard.md at the repository root, resolved from this file (never from
// the process's working directory).
const DOC = fileURLToPath(new URL("../../../docs/dashboard.md", import.meta.url).href);

// Only the explicit generation command sets this (npm run docs:wiring, run in
// GitHub Actions). Ordinary test runs never write the file.
const UPDATE =
  (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env
    ?.UPDATE_WIRING_DOCS === "1";

const doc = (block: string) => `# Title\n\nIntro.\n\n${START_MARKER}\n${block}${END_MARKER}\n\nOutro.\n`;

describe("wiring docs renderer", () => {
  it("escapes table-breaking characters in cells", () => {
    expect(cell("a | b")).toBe("a \\| b");
    expect(cell("line one\nline two\r\nthree")).toBe("line one line two three");
  });

  it("resolves documentation links from docs/dashboard.md", () => {
    expect(docsLink({ ref: "docs/security.md#operator-access", title: "Security" })).toBe(
      "[Security](security.md#operator-access)",
    );
    expect(docsLink({ ref: "README.md#model-lifecycle-block-6", title: "README | lifecycle" })).toBe(
      "[README \\| lifecycle](../README.md#model-lifecycle-block-6)",
    );
  });

  it("lists every wired and local control once, sorted by id", () => {
    const out = renderWiringTable();
    const ids = [...out.matchAll(/^\| [^|]+ \| `([a-z0-9.-]+)` \|/gm)].map((m) => m[1]);
    const wired = [...WIRING.map((w) => w.id)].sort();
    const local = [...LOCAL_CONTROLS.map((c) => c.id)].sort();
    expect(ids).toEqual([...wired, ...local]);
    expect(out).not.toContain("docs/docs/");
    expect(out).toContain("`GET /admin/models/{model_id}`");
    expect(out.endsWith("\n")).toBe(true);
    // Every row has the same number of columns as its header.
    for (const line of out.split("\n").filter((l) => l.startsWith("| "))) {
      const cols = line.replace(/\\\|/g, "").split("|").length - 2;
      expect([8, 4]).toContain(cols);
    }
  });

  it("is deterministic", () => {
    expect(renderWiringTable()).toBe(renderWiringTable());
  });
});

describe("wiring-table markers", () => {
  it("replaces only the block and keeps the surrounding prose", () => {
    const out = replaceBlock(doc("old\n"), "new\n");
    expect(out).toBe(doc("new\n"));
    expect(currentBlock(out)).toBe("new\n");
  });

  it("rejects a missing marker", () => {
    expect(() => replaceBlock("# no markers\n", "x\n")).toThrow(/one start and one end/);
    expect(() => replaceBlock(`${START_MARKER}\nx\n`, "x\n")).toThrow(/one start and one end/);
  });

  it("rejects duplicate markers", () => {
    expect(() => replaceBlock(doc("a\n") + doc("b\n"), "x\n")).toThrow(/found 2 and 2/);
  });

  it("rejects reversed markers", () => {
    expect(() => replaceBlock(`${END_MARKER}\nx\n${START_MARKER}\n`, "x\n")).toThrow(/before the start/);
  });

  it("rejects malformed markers", () => {
    expect(() => replaceBlock(doc("a\n").replace(START_MARKER, "<!-- wiring-table:begin -->"), "x\n")).toThrow(
      /malformed/,
    );
    expect(() => replaceBlock(doc("a\n").replace(END_MARKER, "<!-- wiring-table:end"), "x\n")).toThrow(/malformed/);
  });
});

describe("docs/dashboard.md", () => {
  it(UPDATE ? "regenerates the wiring table (UPDATE_WIRING_DOCS=1)" : "has an up-to-date wiring table", () => {
    const text = readFileSync(DOC, "utf8");
    const expected = renderWiringTable();
    if (UPDATE) {
      const next = replaceBlock(text, expected);
      if (next !== text) writeFileSync(DOC, next, "utf8");
      return;
    }
    // On failure, regenerate in Actions (the "Wiring docs" step prints the
    // diff); never by hand-editing the block.
    expect(currentBlock(text), "docs/dashboard.md wiring table is out of date").toBe(expected);
  });
});
