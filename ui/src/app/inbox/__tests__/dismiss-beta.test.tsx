/**
 * Regression test — "Dismiss" on a beta funnel trade card must actually
 * persist. The inbox service emits intent "dismiss" for beta (shadow
 * decision_funnel) proposals with a trade_proposal source ref, but the page's
 * runAction only mapped approve/reject/ask_deeper_review/execute for
 * trade_proposal — Dismiss fell through as a silent no-op and the card never
 * left the inbox (Ariel, "Buy SOFI (beta)", 2026-07-09).
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { InboxFeedDTO } from "@/lib/api";

const getInbox = vi.fn();
const proposalReject = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getInbox: (...args: unknown[]) => getInbox(...args),
      proposalReject: (...args: unknown[]) => proposalReject(...args),
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

// A beta funnel proposal exactly as the inbox service shapes it
// (argosy/services/inbox/service.py::_adapt_trades, beta branch).
const FEED: InboxFeedDTO = {
  items: [
    {
      id: "trade:1",
      kind: "trade",
      title: "Buy SOFI (beta)",
      why_now: "Calibrating funnel pick.",
      rank_reason: "Expires soon",
      bucket: 1,
      bucket_label: "Overdue or expiring",
      primary_action: {
        intent: "view_reasoning",
        label: "See the reasoning (beta)",
        style: "primary",
        requires_confirmation: false,
      },
      secondary_actions: [
        {
          intent: "dismiss",
          label: "Dismiss",
          style: "secondary",
          requires_confirmation: false,
        },
      ],
      body: { rationale: "beta pick", beta: true },
      due_at: null,
      expires_at: "2026-07-10T15:32:42Z",
      amount_usd: null,
      source_refs: [{ source: "trade_proposal", ref_id: "1" }],
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

const EMPTY_FEED: InboxFeedDTO = {
  ...FEED,
  items: [],
  needs_you_count: 0,
  quiet: false,
};

describe("inbox 'Dismiss' on a beta funnel trade", () => {
  beforeEach(() => {
    getInbox.mockReset();
    proposalReject.mockReset();
  });

  it("rejects the proposal and drops the card on refresh", async () => {
    getInbox.mockResolvedValueOnce(FEED).mockResolvedValue(EMPTY_FEED);
    proposalReject.mockResolvedValue({ status: "ok", proposal_id: 1, message: "" });

    render(<InboxPage />);
    const btn = await screen.findByRole("button", { name: "Dismiss" });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(proposalReject).toHaveBeenCalledWith(1, "ariel", "Dismissed from inbox");
    });
    // The queue refetched and the card is gone — no silent no-op.
    await waitFor(() => {
      expect(screen.queryByText("Buy SOFI (beta)")).not.toBeInTheDocument();
    });
  });

  it("surfaces a reject failure as a visible error", async () => {
    getInbox.mockResolvedValue(FEED);
    proposalReject.mockRejectedValueOnce(new Error("HTTP 409 illegal transition"));

    render(<InboxPage />);
    const btn = await screen.findByRole("button", { name: "Dismiss" });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(screen.getByText(/HTTP 409/)).toBeInTheDocument();
    });
  });
});
