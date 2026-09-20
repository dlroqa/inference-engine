import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Meter, Badge, categoryTone, stateTone } from "../components/widgets";
import { ConnBadge, AsyncBoundary } from "../components/Panel";

describe("Meter", () => {
  it("renders a populated value with accessible attributes", () => {
    render(<Meter label="CPU" percent={42} />);
    const meter = screen.getByRole("meter", { name: "CPU" });
    expect(meter).toHaveAttribute("aria-valuenow", "42");
    expect(screen.getByText("42%")).toBeInTheDocument();
  });

  it("shows an unavailable state when percent is null", () => {
    render(<Meter label="Disk" percent={null} />);
    expect(screen.getByText("unavailable")).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Disk" })).not.toHaveAttribute("aria-valuenow");
  });
});

describe("tone helpers", () => {
  it("maps error categories to tones", () => {
    expect(categoryTone("backend")).toBe("danger");
    expect(categoryTone("validation")).toBe("warn");
    expect(categoryTone("cancellation")).toBe("neutral");
    expect(categoryTone(null)).toBe("neutral");
  });
  it("maps backend states to tones", () => {
    expect(stateTone("ready")).toBe("ok");
    expect(stateTone("failed")).toBe("danger");
    expect(stateTone("unloaded")).toBe("neutral");
  });
});

describe("ConnBadge", () => {
  it("shows the disconnected state", () => {
    render(<ConnBadge status="down" />);
    expect(screen.getByRole("status")).toHaveTextContent("Disconnected");
  });
  it("shows the live state", () => {
    render(<ConnBadge status="live" />);
    expect(screen.getByRole("status")).toHaveTextContent("Live");
  });
});

describe("AsyncBoundary", () => {
  it("renders loading", () => {
    render(
      <AsyncBoundary status="loading">
        <div>content</div>
      </AsyncBoundary>,
    );
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
    expect(screen.queryByText("content")).not.toBeInTheDocument();
  });

  it("renders error with retry", () => {
    render(
      <AsyncBoundary status="error" error="boom" onRetry={() => {}}>
        <div>content</div>
      </AsyncBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("renders empty state", () => {
    render(
      <AsyncBoundary status="ready" isEmpty emptyText="nothing here">
        <div>content</div>
      </AsyncBoundary>,
    );
    expect(screen.getByText("nothing here")).toBeInTheDocument();
  });

  it("renders content when ready and non-empty", () => {
    render(
      <AsyncBoundary status="ready">
        <div>content</div>
      </AsyncBoundary>,
    );
    expect(screen.getByText("content")).toBeInTheDocument();
  });
});
