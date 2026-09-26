import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ConfirmDialog } from "../components/Dialog";
import { buildHash, parseHash } from "../hooks/useHashRoute";

describe("ConfirmDialog", () => {
  const options = { title: "Delete it?", body: "Gone for good.", confirmLabel: "Delete" };

  it("is a labelled modal dialog with Cancel focused first", () => {
    render(<ConfirmDialog options={options} onResult={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Delete it?" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("keeps Tab focus inside the dialog", async () => {
    render(<ConfirmDialog options={options} onResult={vi.fn()} />);
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Delete" })).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Delete" })).toHaveFocus();
  });

  it("confirms, cancels, and treats Escape as cancel", async () => {
    const onResult = vi.fn();
    const { rerender } = render(<ConfirmDialog options={options} onResult={onResult} />);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onResult).toHaveBeenLastCalledWith(true);
    rerender(<ConfirmDialog options={options} onResult={onResult} />);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onResult).toHaveBeenLastCalledWith(false);
    await userEvent.keyboard("{Escape}");
    expect(onResult).toHaveBeenLastCalledWith(false);
  });
});

describe("hash routes", () => {
  it("round-trips views, segments, and params", () => {
    const hash = buildHash("clients", { segments: ["a b"], params: { x: "1", empty: undefined } });
    expect(hash).toBe("#/clients/a%20b?x=1");
    const route = parseHash(hash);
    expect(route.view).toBe("clients");
    expect(route.segments).toEqual(["a b"]);
    expect(route.params.get("x")).toBe("1");
  });

  it.each(["%", "%2", "%GG", "%E0%A4%A", "clients/%", "clients/%FF"])(
    "treats malformed path %s as unavailable without partially decoding it",
    (path) => {
      const route = parseHash(`#/${path}?x=1`);
      expect(route.view).toBe(path);
      expect(route.segments).toEqual([]);
      expect(route.params.get("x")).toBe("1");
    },
  );

  it("preserves valid encoded percent and Unicode segments", () => {
    const route = parseHash(buildHash("clients", { segments: ["100% café"] }));
    expect(route.view).toBe("clients");
    expect(route.segments).toEqual(["100% café"]);
  });

  it("falls back to the default view", () => {
    expect(parseHash("").view).toBe("overview");
    expect(parseHash("#/").view).toBe("overview");
  });
});
