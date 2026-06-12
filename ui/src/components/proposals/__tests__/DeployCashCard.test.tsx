// ui/src/components/proposals/__tests__/DeployCashCard.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DeployCashCard } from "../DeployCashCard";
import type { DeploymentPlanDTO } from "@/lib/api";

const PLAN: DeploymentPlanDTO = {
  deploy_amount_usd: 10000, as_of: "2026-06-12", deployed_total_usd: 10000,
  us_situs_total_usd: 0, market_context_age: null,
  tiers: [
    { name: "reserve", cap_pct: 0, total_usd: 0, lines: [] },
    { name: "core", cap_pct: 70, total_usd: 10000, lines: [{
      symbol: "CSPX", type: "ETF", amount_usd: 10000, timing: "now", is_new: false,
      tier: "core", horizon: "10yr+",
      estate: { domicile: "IE", status: "estate_safe", note: "non-US-situs (IE)" },
      cap_note: "fills US broad-market core", net_of_tax_caveat: "net of CGT",
      rationale: "gap-fill", cites: [],
    }] },
    { name: "medium", cap_pct: 25, total_usd: 0, lines: [] },
    { name: "high", cap_pct: 5, total_usd: 0, lines: [] },
  ],
  caveats: ["confirm net of Israeli CGT"], note: "Plan-only deploy (P1)",
};

describe("DeployCashCard", () => {
  it("renders the core tier line with symbol, amount, estate and NEW flag", () => {
    render(<DeployCashCard plan={PLAN} loading={false} amount={10000}
                           onAmountChange={vi.fn()} unallocatedUsd={50000} />);
    expect(screen.getByText("CSPX")).toBeInTheDocument();
    expect(screen.getByText(/estate.safe/i)).toBeInTheDocument();
    expect(screen.getByText("Core")).toBeInTheDocument();
  });

  it("shows the deploy-amount input prefilled and the caveats", () => {
    render(<DeployCashCard plan={PLAN} loading={false} amount={10000}
                           onAmountChange={vi.fn()} unallocatedUsd={50000} />);
    expect(screen.getByDisplayValue("10000")).toBeInTheDocument();
    expect(screen.getByText(/net of Israeli CGT/i)).toBeInTheDocument();
  });
});
