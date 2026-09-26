import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { WiredTo } from "../components/WiredTo";
import { SUBSYSTEMS, routeFor, wiringFor } from "../lib/wiring";

const LOAD = "How this works: Load model";

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

describe("WiredTo popover", () => {
  it("starts closed with a labelled 44px-class trigger", () => {
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

  it("lists several entries for a control that drives more than one call", async () => {
    await userEvent.click(setup(["monitoring.alerts", "local.navigation"]));
    const pop = screen.getByRole("group");
    expect(pop).toHaveTextContent("GET /admin/alerts");
    expect(pop).toHaveTextContent("Backend call");
    expect(pop).toHaveTextContent("No backend call");
  });

  it("opens on keyboard focus and closes when focus moves on", async () => {
    const trigger = setup();
    await userEvent.tab(); // "before"
    await userEvent.tab(); // the info button
    expect(trigger).toHaveFocus();
    expect(screen.getByRole("group", { name: LOAD })).toBeInTheDocument();
    expect(trigger).toHaveAccessibleDescription(/Load model/);
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "after" })).toHaveFocus();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("closes on Escape and keeps focus on the trigger", async () => {
    const trigger = setup();
    await userEvent.tab();
    await userEvent.tab();
    expect(screen.getByRole("group")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("opens on hover, stays open over the popover, and closes after the pointer leaves", async () => {
    const trigger = setup();
    await userEvent.hover(trigger);
    const pop = await screen.findByRole("group");
    await userEvent.hover(pop);
    await new Promise((r) => setTimeout(r, 300));
    expect(screen.getByRole("group")).toBeInTheDocument();
    await userEvent.unhover(pop);
    await userEvent.hover(screen.getByRole("button", { name: "after" }));
    await waitFor(() => expect(screen.queryByRole("group")).not.toBeInTheDocument());
  });

  it("opens on tap, stays pinned, and closes on a tap outside", async () => {
    const trigger = setup();
    const user = userEvent.setup();
    await user.pointer({ keys: "[TouchA]", target: trigger });
    expect(screen.getByRole("group")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 300));
    expect(screen.getByRole("group")).toBeInTheDocument();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("a click pins it open after hover, and a second click closes it", async () => {
    const trigger = setup();
    await userEvent.click(trigger);
    expect(screen.getByRole("group")).toBeInTheDocument();
    await userEvent.unhover(trigger);
    await new Promise((r) => setTimeout(r, 300));
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

  it("rejects an id that is not in the registry", () => {
    expect(() => render(<WiredTo id="nope.missing" />)).toThrow(/unknown wiring id/);
  });
});
