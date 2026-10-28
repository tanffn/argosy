import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { WholePortfolioComparisonDTO } from "@/lib/api";
import { WholePortfolioComparison } from "../whole-portfolio-comparison";

describe("whole-portfolio comparison", () => {
  it("shows all three prospective paths without inventing returns", () => {
    render(<WholePortfolioComparison data={{ status: "awaiting_baseline", version: "v1",
      position_count: 47, recorded_liquid_usd: "4100000", scheduled_baseline_date: "2026-10-26",
    } as WholePortfolioComparisonDTO} />);
    expect(screen.getByText("Keep recorded portfolio")).toBeTruthy();
    expect(screen.getByText("Follow Argosy (paper)")).toBeTruthy();
    expect(screen.getByText("S&P 500 proxy (SPY)")).toBeTruthy();
    expect(screen.getByText(/2026-10-26/)).toBeTruthy();
    expect(screen.getAllByText("Not yet measurable")).toHaveLength(3);
    expect(screen.queryByText("0.00%")).toBeNull();
  });
  it("calls incomplete data incomplete, not no-action or outperformance", () => {
    render(<WholePortfolioComparison data={{ status: "partial", version: "v1",
      latest: { status: "baseline_incomplete", baseline: { missing: ["bank|ABC"] } },
    } as unknown as WholePortfolioComparisonDTO} />);
    expect(screen.getByText(/Missing holdings have not been dropped/)).toBeTruthy();
    expect(screen.getByText(/No whole-portfolio outperformance is claimed/)).toBeTruthy();
    expect(screen.getByText(/Unpriced baseline components: bank\|ABC/)).toBeTruthy();
    expect(screen.getAllByText("Unknown / pending evidence")).toHaveLength(3);
  });
});
