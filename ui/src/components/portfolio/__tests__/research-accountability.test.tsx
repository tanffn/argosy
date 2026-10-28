import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RecommendationScorecardDTO } from "@/lib/api";
import { RecommendationScorecard } from "../discovery-card";

describe("research accountability browsing", () => {
  it("shows automatic repair progress without calling a research verdict an order", () => {
    const stats = { graded: 0, scheduled: 0, wins: 0, misses: 0, win_rate: null };
    const data = {
      horizons: { "30d": stats, "180d": stats, "365d": stats }, recent: [], coverage: {},
      research_followthrough: [
        { id: "pending", ticker: "XYZ", state: "pending", question: "Verify cash runway" },
        { id: "done", ticker: "ABC", state: "reviewed", question: "Verify catalysts", clock_ids: [1, 2, 3],
          result: { verdict: "WATCH", decision_run_id: 123, thesis_md: "No priced catalyst yet" } },
      ],
    } as unknown as RecommendationScorecardDTO;
    render(<RecommendationScorecard data={data} />);
    expect(screen.getByText(/Automatic research follow-up: 1 open/)).toBeTruthy();
    expect(screen.getByText(/These are not trade orders/)).toBeTruthy();
    expect(screen.getByText("ABC: WATCH")).toBeTruthy();
    expect(screen.getByText(/Fleet run 123 · 3 outcome checkpoints/)).toBeTruthy();
  });
  it("does not hide new research behind historical missing-evidence rows", () => {
    const stats = { graded: 0, scheduled: 0, wins: 0, misses: 0, win_rate: null };
    const row = { recommendation: "PASS", horizon_days: 30, evaluation_date: "2026-10-25",
      entry_price: null, target: "Observe price vs SPY", expectation: "No independent thesis",
      disposition: "research_only_not_a_trade", grade: "pending", report: "Scheduled" };
    const data = {
      horizons: { "30d": stats, "180d": stats, "365d": stats }, recent: [], coverage: {},
      self_evaluations: [
        ...Array.from({ length: 40 }, (_, i) => ({ ...row, prediction_id: i, ticker: `OLD${i}`,
          source: "portfolio_verdict", status: "unscorable", recommended_at: "2026-08-01" })),
        { ...row, prediction_id: 100, ticker: "XYZ", source: "research_review", status: "pending",
          recommended_at: "2026-09-25" },
      ],
    } as unknown as RecommendationScorecardDTO;
    render(<RecommendationScorecard data={data} />);
    fireEvent.click(screen.getByText(/Automatic self-evaluation vs/));
    expect(screen.getByText("XYZ · PASS")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Evaluation source"), { target: { value: "research_review" } });
    expect(screen.getByText("XYZ · PASS")).toBeTruthy();
    expect(screen.queryByText("OLD0 · PASS")).toBeNull();
    fireEvent.change(screen.getByLabelText("Filter evaluations by ticker"), { target: { value: "missing" } });
    expect(screen.queryByText("XYZ · PASS")).toBeNull();
  });
});
