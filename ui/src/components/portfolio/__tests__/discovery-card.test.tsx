import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { DiscoveryDTO } from "@/lib/api";

const portfolioDiscovery = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      portfolioDiscovery: (...args: unknown[]) => portfolioDiscovery(...args),
    },
  };
});

import { DiscoveryCard } from "../discovery-card";

const ZERO_SCORECARD = {
  source: "signal_stream:gov_contracts",
  scored_outcomes: 0,
  win_rate: null,
  avg_pnl_pct: null,
  observation_days: 0,
  calibration: "uncalibrated (beta — 0 scored over 0 days)",
  horizons: {
    "30d": {
      scored_outcomes: 0,
      win_rate: null,
      avg_pnl_pct: null,
    },
    "180d": {
      scored_outcomes: 0,
      win_rate: null,
      avg_pnl_pct: null,
      always_long_same_tickers_win_rate: null,
    },
  },
  funnel_context_enabled: true,
  kill_reason: null,
};

const KILLED_SCORECARD = {
  source: "signal_stream:insider_cluster",
  scored_outcomes: 70,
  win_rate: 0.42,
  avg_pnl_pct: -0.01,
  observation_days: 240,
  calibration: "calibrated",
  horizons: {
    "30d": {
      scored_outcomes: 20,
      win_rate: 0.5,
      avg_pnl_pct: 0.01,
    },
    "180d": {
      scored_outcomes: 50,
      win_rate: 0.4,
      avg_pnl_pct: -0.02,
      always_long_same_tickers_win_rate: 0.4,
    },
  },
  funnel_context_enabled: false,
  kill_reason:
    "180d stream win rate 40.0% does not beat always-long same-tickers benchmark 40.0% (n=50)",
};

const DISCOVERY: DiscoveryDTO = {
  picks: [],
  estimated: [],
  last_refreshed_at: null,
  note: "test",
  stages: {
    tracked: 0,
    active: 0,
    quarantined: 0,
    dropped_stale: 0,
    estimated: 0,
    estimator_go: 0,
    fleet_graded: 0,
    fleet_buy: 0,
    open_trade_proposals: 0,
  },
  candidates: [],
  sources: [
    {
      key: "attention",
      label: "Attention",
      tracked_count: 0,
      active_count: 0,
      quarantined_count: 0,
      dropped_stale_count: 0,
      scorecard: null,
    },
    {
      key: "gov_contracts",
      label: "Government contracts",
      tracked_count: 16,
      active_count: 0,
      quarantined_count: 0,
      dropped_stale_count: 0,
      scorecard: ZERO_SCORECARD,
    },
    {
      key: "insider_cluster",
      label: "Insider cluster",
      tracked_count: 50,
      active_count: 0,
      quarantined_count: 0,
      dropped_stale_count: 0,
      scorecard: KILLED_SCORECARD,
    },
  ],
};

describe("DiscoveryCard signal calibration", () => {
  it("shows research failure separately from investment rejection and both audit tails", async () => {
    const row = { ticker: "XYL", evaluated_decision_at: "2026-08-31", verdict: "PASS",
      gross_return: .5, outcome_date: "2026-09-30", evaluation_method: "entry_backfilled",
      rationale: "Original valuation objection", latest_buy_proposal: null };
    portfolioDiscovery.mockResolvedValueOnce({ ...DISCOVERY,
      research_status: { status: "incomplete", attempted: 1, deferred: 0,
        failures: [{ ticker: "XYZ", reason: "No analyst quorum" }] },
      opportunity_audit: {
        horizon_days: 30, limitations: "Gross diagnostics, not portfolio returns.",
        coverage: { unique_names: 3, fleet_reviewed: 2, radar_without_fleet_grade: 1,
          scored: 2, unscored: 1, skipped_then_rose: 1, skipped_then_fell: 1 },
        recent_research: [], rows: [{ ...row, ticker: "DROPPED", verdict: null,
          evaluated_decision_at: null, gross_return: null, outcome_date: null }], missed_upside_examples: [row],
        avoided_decline_examples: [{ ...row, ticker: "LOSS", gross_return: -.4 }],
      },
    });
    render(<DiscoveryCard />);
    await waitFor(() => expect(screen.getByText(/This is not a conclusion/)).toBeInTheDocument());
    expect(screen.getByText(/3 names observed/)).toBeInTheDocument();
    expect(screen.getByText(/1 subsequently rose, 1 fell/)).toBeInTheDocument();
    expect(screen.getByText(/Gross diagnostics/)).toBeInTheDocument();
    expect(screen.getAllByText(/Backfilled entry/)).toHaveLength(2);
    fireEvent.change(screen.getByLabelText('Find a discovery audit ticker'), { target: { value: 'DROPPED' } });
    expect(screen.getByText(/DROPPED · No fleet verdict/)).toBeInTheDocument();
    expect(screen.getByText(/Outcome unavailable/)).toBeInTheDocument();
  });

  it("shows beta sample counts, horizon details, and paused funnel voice", async () => {
    portfolioDiscovery.mockResolvedValueOnce(DISCOVERY);

    render(<DiscoveryCard />);

    await waitFor(() =>
      expect(screen.getByText(/Government contracts/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/beta 0 scored/i)).toBeInTheDocument();
    expect(screen.getByText(/30d: 0 scored/i)).toBeInTheDocument();
    expect(screen.getByText(/180d: 0 scored/i)).toBeInTheDocument();
    expect(screen.getByText(/funnel voice paused/i)).toBeInTheDocument();
    expect(screen.getByText(/does not beat always-long/i)).toBeInTheDocument();
  });
});
