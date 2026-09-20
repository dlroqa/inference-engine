import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Keys } from "../views/Keys";
import { api, type KeyRow } from "../lib/api";

const key = (over: Partial<KeyRow> = {}): KeyRow => ({
  id: "k1",
  prefix: "sk-ie-ab12",
  label: "app",
  created_at: "2026-09-20T00:00:00+00:00",
  last_used_at: null,
  revoked: false,
  ...over,
});

afterEach(() => vi.restoreAllMocks());

describe("Keys view", () => {
  it("lists existing keys without exposing secrets", async () => {
    vi.spyOn(api, "listKeys").mockResolvedValue({ keys: [key()] });
    render(<Keys />);
    expect(await screen.findByText("sk-ie-ab12…")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
  });

  it("shows the new token exactly once after creation", async () => {
    vi.spyOn(api, "listKeys").mockResolvedValue({ keys: [] });
    vi.spyOn(api, "createKey").mockResolvedValue({
      id: "k2",
      prefix: "sk-ie-zz99",
      label: "new",
      created_at: "2026-09-20T00:00:00+00:00",
      token: "sk-ie-THE-SECRET-TOKEN",
    });
    render(<Keys />);
    await screen.findByText("No API keys yet.");
    await userEvent.type(screen.getByLabelText("Label (optional)"), "new");
    await userEvent.click(screen.getByRole("button", { name: /Create key/ }));
    expect(await screen.findByTestId("new-token")).toHaveTextContent("sk-ie-THE-SECRET-TOKEN");
    expect(screen.getByText(/shown only once/i)).toBeInTheDocument();
  });

  it("revokes an active key", async () => {
    vi.spyOn(api, "listKeys").mockResolvedValue({ keys: [key()] });
    const revoke = vi.spyOn(api, "revokeKey").mockResolvedValue({ revoked: true, id: "k1" });
    render(<Keys />);
    await screen.findByText("sk-ie-ab12…");
    await userEvent.click(screen.getByRole("button", { name: /Revoke key sk-ie-ab12/ }));
    await waitFor(() => expect(revoke).toHaveBeenCalledWith("k1"));
  });

  it("deletes a revoked key (no revoke action shown)", async () => {
    vi.spyOn(api, "listKeys").mockResolvedValue({ keys: [key({ revoked: true })] });
    const del = vi.spyOn(api, "deleteKey").mockResolvedValue({ deleted: true, id: "k1" });
    render(<Keys />);
    await screen.findByText("sk-ie-ab12…");
    expect(screen.getByText("revoked")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Revoke key/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Delete key sk-ie-ab12/ }));
    await waitFor(() => expect(del).toHaveBeenCalledWith("k1"));
  });

  it("shows an error state when the list fails", async () => {
    vi.spyOn(api, "listKeys").mockRejectedValue(new Error("down"));
    render(<Keys />);
    expect(await screen.findByRole("alert")).toHaveTextContent("down");
  });
});
