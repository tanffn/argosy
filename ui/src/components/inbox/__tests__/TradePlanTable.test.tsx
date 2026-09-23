import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TradePlanTable } from "../TradePlanTable";

describe("pending research on the single trade plan", () => {
  it("shows a shared reserve and follow-up, without a buy row for either alternative", () => {
    render(<TradePlanTable plan={{
      as_of: "2026-09-11", book_total_usd: 4000000, source: "order_sheet",
      lines: [], totals: { buys_usd: 0, sells_usd: 0, net_to_cash_usd: 0 }, reserve_usd: 20000,
      pending_research: [{ tickers: ["SYNA", "SYNB"], reserved_usd: 20000,
        next_review_date: "2026-09-12", disagreement: "Valuation disputed", missing_evidence: "Sourced revenue",
        research_question: "Which endpoint is supported?", independence_reason: "Research funding remains intact" }],
    }} />);
    expect(screen.getByText("Research pending — not approved")).toBeInTheDocument();
    expect(screen.getByText(/SYNA \/ SYNB/)).toHaveTextContent("shared reserve");
    expect(screen.getByText(/Automatically revisited/)).toBeInTheDocument();
    expect(screen.queryByText(/approval is blocked/)).not.toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(1); // table header, no executable alternatives
  });
});
