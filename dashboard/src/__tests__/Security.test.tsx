import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Security } from "../views/Security";
import { api } from "../lib/api";

afterEach(() => vi.restoreAllMocks());

describe("Security view", () => {
  it("renders audit records", async () => {
    vi.spyOn(api, "audit").mockResolvedValue({
      events: [
        { id: 1, ts: 1_700_000_000, actor: "local", action: "key.create", target: "k1", detail: null, hash: "abcdef123456ff" },
      ],
    });
    render(<Security />);
    expect(await screen.findByText("key.create")).toBeInTheDocument();
    expect(screen.getByText("local")).toBeInTheDocument();
  });

  it("verifies the hash chain on demand", async () => {
    const spy = vi.spyOn(api, "audit");
    spy.mockResolvedValueOnce({ events: [] });
    spy.mockResolvedValueOnce({ events: [], verify: { ok: true, count: 7, first_bad_id: null } });
    render(<Security />);
    await screen.findByText("No audit records yet.");
    await userEvent.click(screen.getByRole("button", { name: /Verify integrity/ }));
    expect(await screen.findByText(/Hash chain verified — 7 records intact/)).toBeInTheDocument();
  });

  it("shows a tamper warning when verification fails", async () => {
    const spy = vi.spyOn(api, "audit");
    spy.mockResolvedValueOnce({ events: [] });
    spy.mockResolvedValueOnce({ events: [], verify: { ok: false, count: 3, first_bad_id: 2 } });
    render(<Security />);
    await screen.findByText("No audit records yet.");
    await userEvent.click(screen.getByRole("button", { name: /Verify integrity/ }));
    expect(await screen.findByText(/Tamper detected — first bad record id 2/)).toBeInTheDocument();
  });
});
