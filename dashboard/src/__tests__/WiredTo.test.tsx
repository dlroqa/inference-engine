import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WiredTo } from "../components/WiredTo";
import { ConfirmDialog } from "../components/Dialog";
import { DOCS_BASE, SUBSYSTEMS, docsUrl, routeFor, wiringFor } from "../lib/wiring";
import { SystemProvider } from "../hooks/useSystem";
import { WiringPrefsProvider } from "../hooks/useWiringPrefs";
import { api, ApiError, type SystemInfo } from "../lib/api";

const LOAD = "How this works: Load model";

// Lets the hover open/close timers run inside act().
const pause = (ms: number) => act(() => new Promise<void>((r) => setTimeout(r, ms)));

function setup(id: string | string[] = "models.load") {
  render(
    <>
      <button>before</button>
      <WiredTo id={id} />
      <button>after</button>
    </>,
  );
  return screen.getByRole("button", { name: /^How this works/ });
}

async function tabToHint() {
  await userEvent.tab(); // "before"
  await userEvent.tab(); // the info button
}

afterEach(() => vi.restoreAllMocks());

describe("WiredTo popover: content", () => {
  it("starts closed with a labelled trigger", () => {
    const trigger = setup();
    expect(trigger).toHaveAccessibleName(LOAD);
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveClass("wired-btn");
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("shows the route, backend function, access, and chain from the registry", async () => {
    const trigger = setup();
    await userEvent.click(trigger);
    const pop = screen.getByRole("group", { name: LOAD });
    const entry = wiringFor("models.load");
    const route = routeFor(entry);
    expect(route).toBeDefined();
    expect(pop).toHaveTextContent("Backend call");
    expect(pop).toHaveTextContent("POST /admin/models/{model_id}/load");
    expect(pop).toHaveTextContent(`${route!.module}.${route!.handler}`);
    expect(pop).toHaveTextContent("Operator key");
    expect(pop).toHaveTextContent("api.loadModelById()");
    expect(pop).toHaveTextContent("allow_model_management");
    expect(pop).toHaveTextContent("Recorded");
    const chain = within(pop).getByRole("list", { name: "Subsystems, in order" });
    expect(within(chain).getAllByRole("listitem").map((li) => li.textContent)).toEqual(
      entry.chain.map((s) => SUBSYSTEMS[s].label),
    );
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("aria-controls", pop.id);
    // The panel holds interactive content, so it is not used as a description.
    expect(trigger).not.toHaveAttribute("aria-describedby");
  });

  it("marks controls with no backend call", async () => {
    const trigger = setup("local.logs-filter");
    expect(trigger).toHaveAccessibleName("How this works: Text filter (no backend call)");
    expect(trigger).toHaveClass("local");
    await userEvent.click(trigger);
    const pop = screen.getByRole("group");
    expect(pop).toHaveTextContent("No backend call");
    expect(pop).toHaveTextContent("No request is sent to the engine");
    expect(pop).not.toHaveTextContent("Route");
    expect(pop).not.toHaveTextContent("Backend function");
  });

  it("says 'no direct engine call' when a local action leads to a request", async () => {
    const trigger = setup("local.saved-key");
    expect(trigger).toHaveAccessibleName("How this works: Saved operator key (no direct engine call)");
    await userEvent.click(trigger);
    const pop = screen.getByRole("group");
    expect(pop).toHaveTextContent("No direct engine call");
    expect(pop).toHaveTextContent("GET /admin/identity");
    expect(pop).toHaveTextContent("Bearer token");
    expect(pop).not.toHaveTextContent("No request is sent to the engine");
  });

  it("lists several entries for a control that drives more than one call", async () => {
    await userEvent.click(setup(["monitoring.alerts", "local.navigation"]));
    const pop = screen.getByRole("group");
    expect(pop).toHaveTextContent("GET /admin/alerts");
    expect(pop).toHaveTextContent("Backend call");
    expect(pop).toHaveTextContent("No direct engine call");
  });

  it("rejects an id that is not in the registry", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<WiredTo id="nope.missing" />)).toThrow(/unknown wiring id/);
  });
});

describe("WiredTo popover: switches and model facts", () => {
  it("download names both required switches and the conditional checksum", async () => {
    await userEvent.click(setup("models.download"));
    const pop = screen.getByRole("group");
    expect(pop).toHaveTextContent("Kill switches");
    expect(pop).toHaveTextContent("allow_model_management");
    expect(pop).toHaveTextContent("allow_network_downloads");
    expect(pop).toHaveTextContent("Both must be on; turning off either one disables this action.");
    expect(within(pop).getAllByRole("listitem").map((li) => li.textContent)).toEqual(
      expect.arrayContaining(["allow_model_management", "allow_network_downloads"]),
    );
    expect(pop).toHaveTextContent("calculates its SHA-256");
    expect(pop).toHaveTextContent("When you provide an expected SHA-256, the download is checked against it");
    expect(pop).not.toHaveTextContent(/checksum-verified/i);
  });

  it("a single-switch action shows one switch and no conjunction", async () => {
    await userEvent.click(setup("models.load"));
    const pop = screen.getByRole("group");
    expect(pop).toHaveTextContent("Kill switch");
    expect(pop).not.toHaveTextContent("Kill switches");
    expect(pop).toHaveTextContent("allow_model_management");
    expect(pop).not.toHaveTextContent("allow_network_downloads");
    expect(pop).not.toHaveTextContent("Both must be on");
  });

  it("an action without switches has no switch section", async () => {
    await userEvent.click(setup("models.list"));
    expect(screen.getByRole("group")).not.toHaveTextContent(/Kill switch/);
  });

  it("delete distinguishes engine-managed files from external imports", async () => {
    await userEvent.click(setup("models.delete"));
    const pop = screen.getByRole("group");
    expect(pop).toHaveTextContent("Files managed by the engine are deleted");
    expect(pop).toHaveTextContent("imported files outside the model store remain on disk");
    expect(pop).toHaveTextContent("An external imported file is preserved and can be registered again by importing it.");
    expect(pop).not.toHaveTextContent("must be downloaded or imported again");
  });
});

describe("WiredTo popover: documentation links", () => {
  it("renders an accessible new-tab link to the repository docs", async () => {
    await userEvent.click(setup("app.identity"));
    const link = within(screen.getByRole("group")).getByRole("link", {
      name: "Security: operator access (opens in a new tab)",
    });
    expect(link).toHaveAttribute("href", `${DOCS_BASE}docs/security.md#operator-access`);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("rel")).toContain("noreferrer");
  });

  it("resolves README and nested docs references with fragments", () => {
    expect(docsUrl(wiringFor("models.list").docs!)).toBe(`${DOCS_BASE}README.md#model-lifecycle-block-6`);
    expect(docsUrl(wiringFor("security.audit").docs!)).toBe(
      `${DOCS_BASE}docs/security.md#tamper-evident-audit-log`,
    );
    expect(DOCS_BASE).toBe("https://github.com/dlroqa/inference-engine/blob/main/");
  });

  it("refuses references that are not plain repository markdown paths", () => {
    for (const ref of ["../secrets.md", "https://evil.example/x.md", "docs/a.md?key=1", "docs/a.txt"]) {
      expect(() => docsUrl({ ref, title: "x" })).toThrow(/invalid docs reference/);
    }
  });
});

describe("WiredTo popover: keyboard", () => {
  it("opens on keyboard focus, then Tab moves into the panel and its link without closing", async () => {
    const trigger = setup("app.identity");
    await tabToHint();
    expect(trigger).toHaveFocus();
    const pop = screen.getByRole("group");
    await userEvent.tab();
    expect(pop).toHaveFocus();
    await userEvent.tab();
    expect(within(pop).getByRole("link")).toHaveFocus();
    expect(screen.getByRole("group")).toBeInTheDocument();
    // Leaving the whole region closes it.
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "after" })).toHaveFocus();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("Shift+Tab from the panel back to the button keeps it open", async () => {
    const trigger = setup();
    await tabToHint();
    await userEvent.tab();
    await userEvent.tab({ shift: true });
    expect(trigger).toHaveFocus();
    expect(screen.getByRole("group")).toBeInTheDocument();
  });

  it("Escape from the button closes it and keeps focus there", async () => {
    const trigger = setup();
    await tabToHint();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("Escape from inside the panel returns focus to the button and stays closed", async () => {
    const trigger = setup("app.identity");
    await tabToHint();
    await userEvent.tab();
    await userEvent.tab();
    expect(within(screen.getByRole("group")).getByRole("link")).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    await pause(300);
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    // Enter reopens it deliberately.
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("group")).toBeInTheDocument();
  });

  it("inside a dialog, Escape closes the popover before the dialog", async () => {
    const onResult = vi.fn();
    render(
      <ConfirmDialog
        options={{ title: "Delete it?", body: "Gone.", confirmLabel: "Delete", wiring: "models.delete" }}
        onResult={onResult}
      />,
    );
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await userEvent.tab(); // Delete
    await userEvent.tab(); // the hint, after the actions
    const hint = screen.getByRole("button", { name: "How this works: Delete" });
    expect(hint).toHaveFocus();
    expect(screen.getByRole("group")).toHaveTextContent("DELETE /admin/models/{model_id}");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(onResult).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(onResult).toHaveBeenLastCalledWith(false);
  });
});

describe("WiredTo popover: Escape precedence with dialogs", () => {
  const deleteOptions = { title: "Delete it?", body: "Gone.", confirmLabel: "Delete", wiring: "models.delete" };

  it("a hover preview inside a dialog takes the first Escape; focus stays on Cancel", async () => {
    const onResult = vi.fn();
    render(<ConfirmDialog options={deleteOptions} onResult={onResult} />);
    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel).toHaveFocus();
    const hint = screen.getByRole("button", { name: "How this works: Delete" });
    await userEvent.hover(hint);
    await pause(200);
    expect(screen.getByRole("group")).toBeInTheDocument();
    expect(cancel).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onResult).not.toHaveBeenCalled();
    expect(cancel).toHaveFocus();
    // The pointer has not moved: the preview stays dismissed past the hover delay.
    await pause(400);
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    // The second Escape cancels the dialog as usual.
    await userEvent.keyboard("{Escape}");
    expect(onResult).toHaveBeenCalledTimes(1);
    expect(onResult).toHaveBeenLastCalledWith(false);
  });

  it("a closed popover does not suppress the dialog's Escape", async () => {
    const onResult = vi.fn();
    render(<ConfirmDialog options={deleteOptions} onResult={onResult} />);
    await userEvent.keyboard("{Escape}");
    expect(onResult).toHaveBeenLastCalledWith(false);
  });

  it("a popover behind the modal does not take Escape meant for the dialog", async () => {
    const onResult = vi.fn();
    render(
      <>
        <WiredTo id="models.list" />
        <ConfirmDialog options={deleteOptions} onResult={onResult} />
      </>,
    );
    await userEvent.hover(screen.getByRole("button", { name: "How this works: Models" }));
    await pause(200);
    expect(screen.getByRole("group")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(onResult).toHaveBeenLastCalledWith(false);
  });
});

describe("WiredTo popover: pointer and touch", () => {
  it("opens on hover, stays open over the popover, and closes after the pointer leaves", async () => {
    const trigger = setup();
    await userEvent.hover(trigger);
    await pause(200);
    const pop = screen.getByRole("group");
    await userEvent.hover(pop);
    await pause(300);
    expect(screen.getByRole("group")).toBeInTheDocument();
    await userEvent.unhover(pop);
    await userEvent.hover(screen.getByRole("button", { name: "after" }));
    await pause(300);
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("stays open when the mouse leaves while keyboard focus is inside", async () => {
    const trigger = setup();
    await tabToHint();
    await userEvent.hover(trigger);
    await userEvent.unhover(trigger);
    await pause(300);
    expect(screen.getByRole("group")).toBeInTheDocument();
  });

  it("a hover preview closes on Escape without taking focus from another control", async () => {
    const trigger = setup();
    screen.getByRole("button", { name: "before" }).focus();
    await userEvent.hover(trigger);
    await pause(200);
    expect(screen.getByRole("group")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "before" })).toHaveFocus();
  });

  it("opens on tap, stays pinned, and closes on a tap outside without moving focus", async () => {
    const trigger = setup();
    const user = userEvent.setup();
    await user.pointer({ keys: "[TouchA]", target: trigger });
    expect(screen.getByRole("group")).toBeInTheDocument();
    await pause(300);
    expect(screen.getByRole("group")).toBeInTheDocument();
    const focused = document.activeElement;
    fireEvent.pointerDown(screen.getByRole("button", { name: "after" }));
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(focused);
  });

  it("a click pins it open after hover, and a second click closes it", async () => {
    const trigger = setup();
    await userEvent.click(trigger);
    expect(screen.getByRole("group")).toBeInTheDocument();
    await userEvent.unhover(trigger);
    await pause(300);
    expect(screen.getByRole("group")).toBeInTheDocument();
    await userEvent.click(trigger);
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("clicking inside the popover does not close it", async () => {
    const trigger = setup();
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole("group"));
    expect(screen.getByRole("group")).toBeInTheDocument();
  });
});

describe("WiredTo popover: placement inside the viewport", () => {
  // jsdom has no layout, so the button's box and the content height are supplied.
  function geometry(viewportHeight: number, triggerTop: number, contentHeight: number) {
    const saved = window.innerHeight;
    Object.defineProperty(window, "innerHeight", { configurable: true, value: viewportHeight });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const top = this.classList.contains("wired-btn") ? triggerTop : 0;
      const h = this.classList.contains("wired-btn") ? 28 : 0;
      return { top, bottom: top + h, left: 100, right: 128, width: 28, height: h, x: 100, y: top, toJSON() {} } as DOMRect;
    });
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(contentHeight);
    return () => Object.defineProperty(window, "innerHeight", { configurable: true, value: saved });
  }

  function box() {
    const pop = screen.getByRole("group");
    return { top: parseFloat(pop.style.top), maxHeight: parseFloat(pop.style.maxHeight) };
  }

  it("uses the space below when the content fits", async () => {
    const restore = geometry(900, 100, 300);
    await userEvent.click(setup());
    expect(box()).toEqual({ top: 136, maxHeight: 520 });
    restore();
  });

  it("flips above when only above fits", async () => {
    const restore = geometry(900, 700, 300);
    await userEvent.click(setup());
    expect(box().top).toBe(700 - 8 - 300);
    restore();
  });

  it("uses the roomier side, shortened and scrollable, when neither side fits", async () => {
    const restore = geometry(420, 196, 900);
    await userEvent.click(setup());
    const { top, maxHeight } = box();
    // above = 196 - 8 - 16 = 172; below = 420 - 16 - 232 = 172
    expect(maxHeight).toBe(172);
    expect(top).toBeGreaterThanOrEqual(16);
    expect(top + maxHeight).toBeLessThanOrEqual(420 - 16);
    restore();
  });

  it("clamps over the page when neither side is usable, within all edges", async () => {
    const restore = geometry(300, 136, 900);
    await userEvent.click(setup());
    const { top, maxHeight } = box();
    expect(top).toBe(16);
    expect(maxHeight).toBe(300 - 32);
    restore();
  });

  it("re-places on resize without leaving the viewport", async () => {
    const restore = geometry(900, 400, 300);
    await userEvent.click(setup());
    expect(box().top).toBe(436);
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 500 });
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });
    const { top, maxHeight } = box();
    expect(top).toBeGreaterThanOrEqual(16);
    expect(top + Math.min(300, maxHeight)).toBeLessThanOrEqual(500 - 16);
    restore();
  });

  it("does not re-place when the panel itself scrolls", async () => {
    const restore = geometry(900, 100, 300);
    await userEvent.click(setup());
    const pop = screen.getByRole("group");
    const before = pop.getAttribute("style");
    // A page scroll would now re-place it (a shorter viewport); the panel's own
    // scroll must not.
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 500 });
    act(() => {
      pop.dispatchEvent(new Event("scroll"));
    });
    expect(pop.getAttribute("style")).toBe(before);
    act(() => {
      window.dispatchEvent(new Event("scroll"));
    });
    expect(pop.getAttribute("style")).not.toBe(before);
    restore();
  });
});

describe("WiredTo: Show wiring chips", () => {
  function withChips(ui: React.ReactNode) {
    localStorage.setItem("ie.dashboard.showWiring", "1");
    try {
      return render(<WiringPrefsProvider>{ui}</WiringPrefsProvider>);
    } finally {
      localStorage.removeItem("ie.dashboard.showWiring");
    }
  }

  it("shows no chips by default", () => {
    render(
      <WiringPrefsProvider>
        <WiredTo id="models.load" />
      </WiringPrefsProvider>,
    );
    expect(screen.queryByTestId("wiring-chips")).not.toBeInTheDocument();
  });

  it("shows the registry endpoint for a wired hint", () => {
    withChips(<WiredTo id="models.load" />);
    const codes = Array.from(screen.getByTestId("wiring-chips").querySelectorAll("code")).map((c) => c.textContent);
    expect(codes).toEqual(["POST /admin/models/{model_id}/load"]);
  });

  it("shows every wired endpoint of a multi-entry hint, once each, and none for local entries", () => {
    withChips(<WiredTo id={["overview.metrics", "overview.metrics-fallback", "models.list", "local.navigation"]} />);
    const codes = Array.from(screen.getByTestId("wiring-chips").querySelectorAll("code")).map((c) => c.textContent);
    expect(codes).toEqual(["WS /ws/metrics", "GET /metrics", "GET /admin/models"]);
  });

  it("gives local-only hints no chip", () => {
    withChips(<WiredTo id={["local.navigation", "local.copy-token"]} />);
    expect(screen.queryByTestId("wiring-chips")).not.toBeInTheDocument();
  });

  it("keeps the popover behaviour with chips shown", async () => {
    withChips(
      <>
        <button>before</button>
        <WiredTo id="models.load" />
        <button>after</button>
      </>,
    );
    await tabToHint();
    expect(screen.getByRole("group", { name: LOAD })).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });
});

describe("WiredTo: live switch state", () => {
  const info = (management: boolean) =>
    ({
      switches: {
        allow_model_management: management,
        allow_network_downloads: true,
        allow_structured_output: true,
        diagnostics_enabled: true,
        require_auth: true,
        webhooks_enabled: false,
        client_events_enabled: true,
        ip_allowlist_set: false,
        grpc_enabled: false,
      },
    }) as SystemInfo;

  it("shows current on/off state once /admin/system has answered", async () => {
    vi.spyOn(api, "system").mockResolvedValue(info(false));
    render(
      <SystemProvider>
        <WiredTo id="models.download" />
      </SystemProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: /^How this works/ }));
    await waitFor(() =>
      expect(within(screen.getByRole("group")).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
        "allow_model_management — currently off",
        "allow_network_downloads — currently on",
      ]),
    );
  });

  it("states nothing about switch state while it is unknown", async () => {
    vi.spyOn(api, "system").mockRejectedValue(new ApiError("down", 500));
    render(
      <SystemProvider>
        <WiredTo id="models.load" />
      </SystemProvider>,
    );
    await waitFor(() => expect(api.system).toHaveBeenCalled());
    await act(async () => {});
    await userEvent.click(screen.getByRole("button", { name: /^How this works/ }));
    const pop = screen.getByRole("group");
    expect(pop).toHaveTextContent("allow_model_management");
    expect(pop).not.toHaveTextContent(/currently (on|off)/);
  });
});
