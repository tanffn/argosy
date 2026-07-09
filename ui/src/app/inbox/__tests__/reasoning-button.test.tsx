/**
 * Regression test — "See the reasoning" on a trade item must actually open
 * the reasoning trail ON SCREEN. The click fetched the detail and mounted
 * the trail card below the whole queue, off-viewport, so with 9 pending
 * proposals it looked like a dead button (Ariel, 2026-07-09).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { InboxFeedDTO, ProposalDetail } from "@/lib/api";

const getInbox = vi.fn();
const proposalDetail = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getInbox: (...args: unknown[]) => getInbox(...args),
      proposalDetail: (...args: unknown[]) => proposalDetail(...args),
      portfolioUnallocatedCashProposal: () => Promise.resolve(null),
    },
  };
});

vi.mock("@/lib/ws", () => ({ useWSEvents: () => null }));

// The secondary zones make their own network calls on mount — out of scope.
vi.mock("@/components/proposals/funnel-transparency-card", () => ({
  FunnelTransparencyCard: () => null,
}));
vi.mock("@/components/proposals/DeployCashCard", () => ({ DeployCashCard: () => null }));
vi.mock("@/components/proposals/YourMoveCard", () => ({ YourMoveCard: () => null }));
vi.mock("@/components/proposals/FunnelBetaCard", () => ({ FunnelBetaCard: () => null }));
vi.mock("@/components/proposals/RebalanceReviewCard", () => ({
  RebalanceReviewCard: () => null,
}));
vi.mock("@/components/consult/consult-runner", () => ({ ConsultRunner: () => null }));
vi.mock("@/components/portfolio/discovery-card", () => ({ DiscoveryCard: () => null }));
vi.mock("@/components/portfolio/trend-radar-card", () => ({ TrendRadarCard: () => null }));
vi.mock("@/components/portfolio/speculative-monitor-card", () => ({
  SpeculativeMonitorCard: () => null,
}));
vi.mock("@/components/portfolio/unallocated-cash-card", () => ({
  UnallocatedCashCard: () => null,
}));
vi.mock("@/components/retirement/WindfallCard", () => ({ WindfallCard: () => null }));

import InboxPage from "../page";

const FEED: InboxFeedDTO = {
  items: [
    {
      id: "trade:2",
      kind: "trade",
      title: "Sell NOW",
      why_now: "Verdict: redeploy…",
      rank_reason: "Expires soon",
      bucket: 1,
      bucket_label: "Overdue or expiring",
      primary_action: {
        intent: "approve",
        label: "Approve",
        style: "primary",
        requires_confirmation: false,
      },
      secondary_actions: [
        { intent: "reject", label: "Reject", style: "danger", requires_confirmation: false },
        {
          intent: "view_reasoning",
          label: "See the reasoning",
          style: "secondary",
          requires_confirmation: false,
        },
      ],
      body: { rationale: "Verdict: sell. Recommendation: one tranche." },
      due_at: null,
      expires_at: null,
      amount_usd: null,
      source_refs: [{ source: "trade_proposal", ref_id: "2" }],
      trace: null,
    },
  ],
  quiet: false,
  needs_you_count: 1,
  liveness: {
    last_checked: "2026-07-09T00:00:00Z",
    pending_decisions: 1,
    open_approvals: 1,
    cash_within_band: true,
    no_overdue_tasks: true,
    next_review: null,
  },
  policy_version: "v1",
  generated_at: "2026-07-09T00:00:00Z",
  dropped: [],
};

const DETAIL: ProposalDetail = {
  proposal: {
    id: 2,
    user_id: "ariel",
    ticker: "NOW",
    action: "sell",
    size_shares_or_currency: 8305,
    size_units: "currency",
    instrument: "stock",
    order_type: "market",
    tier: "T2",
    account_class: "main",
    status: "awaiting_human",
    rationale_summary: "Verdict: sell.",
    confidence: "MEDIUM",
    cooling_off_until: null,
    created_at: "2026-07-09T06:25:05Z",
    updated_at: "2026-07-09T06:25:05Z",
    conviction: "MEDIUM",
    cited_sources: [],
  },
  expected_impact: null,
  history: [],
  approvals: [],
  reasoning_trail: [
    {
      id: 2163,
      agent_role: "trader",
      model: "claude-opus-4-8",
      confidence: "MEDIUM",
      response_text: "trader verdict text",
      created_at: "2026-07-09T06:19:37Z",
    },
  ],
  decision_run: { id: 157, ticker: "NOW", status: "approved" },
};

describe("inbox 'See the reasoning'", () => {
  beforeEach(() => {
    getInbox.mockResolvedValue(FEED);
    proposalDetail.mockResolvedValue(DETAIL);
  });

  it("opens the reasoning trail and scrolls it into view", async () => {
    const scrollIntoView = vi.fn();
    // jsdom has no scrollIntoView; the page guards with ?. — install a spy.
    Element.prototype.scrollIntoView = scrollIntoView;

    render(<InboxPage />);
    const btn = await screen.findByRole("button", { name: "See the reasoning" });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(screen.getByText("Reasoning trail")).toBeInTheDocument();
    });
    expect(proposalDetail).toHaveBeenCalledWith("ariel", 2);
    expect(screen.getByText(/trader verdict text/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view full replay/ })).toHaveAttribute(
      "href",
      "/decisions/157",
    );
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
  });

  it("surfaces a fetch failure as a visible error instead of a dead button", async () => {
    proposalDetail.mockRejectedValueOnce(new Error("HTTP 500 from /api/proposals/2"));
    Element.prototype.scrollIntoView = vi.fn();

    render(<InboxPage />);
    const btn = await screen.findByRole("button", { name: "See the reasoning" });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(screen.getByText(/HTTP 500/)).toBeInTheDocument();
    });
    expect(screen.queryByText("Reasoning trail")).not.toBeInTheDocument();
  });
});
