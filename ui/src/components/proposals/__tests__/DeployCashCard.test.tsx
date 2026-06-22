// ui/src/components/proposals/__tests__/DeployCashCard.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DeployCashCard } from "../DeployCashCard";
import type { DeploymentMarketContextDTO, DeploymentPlanDTO } from "@/lib/api";

// The card prefetches prior allocation decisions on mount to render the
// inline Accepted/Deferred pills. Stub the list endpoint so these unit tests
// stay offline; an empty action list means every line shows Accept/Defer.
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      proposalAllocationActionsList: vi.fn().mockResolvedValue({ actions: [] }),
    },
  };
});

const PLAN: DeploymentPlanDTO = {
  deploy_amount_usd: 10000, as_of: "2026-06-12", deployed_total_usd: 10000,
  us_situs_exposed_usd: 0, us_situs_sanctioned_usd: 0, undeployed_remainder_usd: 0,
  market_context_age: null,
  market_context: null,
  tiers: [
    { name: "reserve", cap_pct: 0, total_usd: 0, lines: [] },
    { name: "core", cap_pct: 70, total_usd: 10000, lines: [{
      symbol: "CSPX", type: "ETF", amount_usd: 10000, timing: "now", is_new: false,
      tier: "core", horizon: "10yr+",
      estate: { domicile: "IE", status: "estate_safe", note: "non-US-situs (IE)" },
      cap_note: "fills US broad-market core", net_of_tax_caveat: "net of CGT",
      rationale: "gap-fill", cites: [], held_value_usd: 47890,
      pace_rationale: "",
    }] },
    { name: "medium", cap_pct: 25, total_usd: 0, lines: [] },
    { name: "high", cap_pct: 5, total_usd: 0, lines: [] },
  ],
  caveats: ["confirm net of Israeli CGT"], note: "Plan-only deploy (P1)",
};

const MARKET_CONTEXT: DeploymentMarketContextDTO = {
  snapshot: { sp500: 5432.1, vix: 18.7, usd_nis: 3.71 },
  freshness: [
    { field: "sp500", fetched_at: "2026-06-12T10:00:00Z", age_seconds: 300, source: "fred", is_stale: false },
    { field: "vix", fetched_at: "2026-06-12T10:00:00Z", age_seconds: 300, source: "fred", is_stale: false },
    { field: "usd_nis", fetched_at: "2026-06-12T09:00:00Z", age_seconds: 3700, source: "boi", is_stale: false },
  ],
  nvda: {
    price: 131.5,
    shares: 24_400_000_000,
    market_cap: 3_220_000_000_000,
    consistent: true,
    note: "marketCap/shares within 10% of price",
  },
  overall_age_label: "5m ago",
  is_any_stale: false,
};

const STALE_CONTEXT: DeploymentMarketContextDTO = {
  ...MARKET_CONTEXT,
  is_any_stale: true,
  freshness: [
    { field: "sp500", fetched_at: "2026-06-12T10:00:00Z", age_seconds: 100000, source: "fred", is_stale: true },
  ],
};

const PLAN_WITH_DCA: DeploymentPlanDTO = {
  ...PLAN,
  tiers: [
    { name: "reserve", cap_pct: 0, total_usd: 0, lines: [] },
    { name: "core", cap_pct: 70, total_usd: 20000, lines: [{
      symbol: "CSPX", type: "ETF", amount_usd: 20000, timing: "DCA 6wk", is_new: false,
      tier: "core", horizon: "10yr+",
      estate: { domicile: "IE", status: "estate_safe", note: "non-US-situs (IE)" },
      cap_note: "fills US broad-market core", net_of_tax_caveat: "net of CGT",
      rationale: "gap-fill", cites: [], held_value_usd: 47890,
      pace_rationale: "VIX z-score 1.3 + price at 95th pctile of 52w range → DCA over 6 weeks",
    }] },
    { name: "medium", cap_pct: 25, total_usd: 0, lines: [] },
    { name: "high", cap_pct: 5, total_usd: 0, lines: [] },
  ],
};

describe("DeployCashCard", () => {
  it("renders the core tier line with symbol, amount, estate and NEW flag", () => {
    render(<DeployCashCard plan={PLAN} loading={false} amount={10000}
                           onAmountChange={vi.fn()} unallocatedUsd={50000} userId="ariel" />);
    expect(screen.getByText("CSPX")).toBeInTheDocument();
    expect(screen.getByText(/estate.safe/i)).toBeInTheDocument();
    expect(screen.getByText("Core")).toBeInTheDocument();
  });

  it("shows the deploy-amount input prefilled and the caveats", () => {
    render(<DeployCashCard plan={PLAN} loading={false} amount={10000}
                           onAmountChange={vi.fn()} unallocatedUsd={50000} userId="ariel" />);
    expect(screen.getByDisplayValue("10000")).toBeInTheDocument();
    expect(screen.getByText(/net of Israeli CGT/i)).toBeInTheDocument();
  });

  it("renders MarketContextStrip with age label, snapshot values, and NVDA when market_context is present", () => {
    render(
      <DeployCashCard
        plan={{ ...PLAN, market_context: MARKET_CONTEXT }}
        loading={false}
        amount={10000}
        onAmountChange={vi.fn()}
        unallocatedUsd={50000}
        userId="ariel"
      />
    );
    // Strip is rendered
    expect(screen.getByTestId("market-context-strip")).toBeInTheDocument();
    // Overall age label (multiple elements may contain the age text; at least one present)
    expect(screen.getAllByText(/5m ago/i).length).toBeGreaterThan(0);
    // Snapshot value (S&P 500)
    expect(screen.getByText("S&P 500:")).toBeInTheDocument();
    // NVDA verification block
    expect(screen.getByTestId("nvda-verification")).toBeInTheDocument();
    expect(screen.getByText(/\$131/)).toBeInTheDocument();
    // No stale badge when not stale
    expect(screen.queryByTestId("stale-badge")).not.toBeInTheDocument();
  });

  it("shows a loud STALE DATA badge when is_any_stale is true", () => {
    render(
      <DeployCashCard
        plan={{ ...PLAN, market_context: STALE_CONTEXT }}
        loading={false}
        amount={10000}
        onAmountChange={vi.fn()}
        unallocatedUsd={50000}
        userId="ariel"
      />
    );
    expect(screen.getByTestId("stale-badge")).toBeInTheDocument();
    expect(screen.getByText("STALE DATA")).toBeInTheDocument();
  });

  it("renders DCA timing and pace_rationale for a line with timing 'DCA 6wk'", () => {
    render(
      <DeployCashCard
        plan={PLAN_WITH_DCA}
        loading={false}
        amount={20000}
        onAmountChange={vi.fn()}
        unallocatedUsd={50000}
        userId="ariel"
      />
    );
    expect(screen.getByText("DCA 6wk")).toBeInTheDocument();
    expect(screen.getByTestId("pace-rationale-CSPX")).toBeInTheDocument();
    expect(screen.getByText(/VIX z-score/i)).toBeInTheDocument();
  });

  it("renders the live-context toggle when onLiveChange is supplied", () => {
    const onLiveChange = vi.fn();
    render(
      <DeployCashCard
        plan={PLAN}
        loading={false}
        amount={10000}
        onAmountChange={vi.fn()}
        unallocatedUsd={50000}
        userId="ariel"
        live={false}
        onLiveChange={onLiveChange}
      />
    );
    expect(screen.getByTestId("live-toggle")).toBeInTheDocument();
  });

  it("does not render MarketContextStrip when market_context is null", () => {
    render(
      <DeployCashCard
        plan={PLAN}
        loading={false}
        amount={10000}
        onAmountChange={vi.fn()}
        unallocatedUsd={50000}
        userId="ariel"
      />
    );
    expect(screen.queryByTestId("market-context-strip")).not.toBeInTheDocument();
  });
});
