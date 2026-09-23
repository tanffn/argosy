import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type E2EOrderLineDTO, type E2EProofDTO, type ManualFillResponse } from "@/lib/api";
import { ArgosyRunCard, ManualFillForm } from "../ArgosyRunCard";

afterEach(() => vi.restoreAllMocks());
beforeEach(() => {
  // Do not return the spy: Vitest treats returned functions as teardown hooks.
  vi.spyOn(api, "fillsList").mockResolvedValue({ rows: [], total: 0 });
});

const proof: E2EProofDTO = {
  user_id: "test", stage: "no_action", headline: "No trades proposed; research remains open",
  known_accounts: [], lines: [], no_action: [], self_audit: [],
  checks: [{ key: "fills", label: "Broker fills reconciled", passed: false, applicable: false }],
  artifact: {
    action_proposal_id: 1, status: "open", execution_state: "proposed",
    generated_at: "2026-09-22T00:00:00Z", surfaced_at: "2026-09-22T00:00:00Z",
    expires_at: "2026-09-29T00:00:00Z", fingerprint: "test-fingerprint",
    funding: { new_cash_usd: 0, gross_sell_proceeds_usd: 0, sell_tax_usd: 0,
      sell_costs_usd: 0, reserve_usd: 0, rounding_residual_usd: 0, available_to_buy_usd: 0 },
    rationale: "Research remains incomplete.", horizon_years: [1, 5],
    validation: { valid: true, failures: [] },
    team_telemetry: { decision_ids: [], total_cost_usd: 0, reports: [] },
  },
};

describe("order sheet status", () => {
  it.each([["", null], ["0", 0], ["1.25", 1.25]])("preserves receipt fee omission: %s", async (input, expected) => {
    const capture = vi.spyOn(api, "proposalManualFill").mockResolvedValue({} as ManualFillResponse);
    const line = { proposal: { id: 7, target_quantity: 2, account_id: "Leumi" },
      execution: { filled_quantity: 0, broker_order_id: null } } as E2EOrderLineDTO;
    const saved = vi.fn().mockResolvedValue(undefined);
    render(<ManualFillForm line={line} userId="test" onSaved={saved} />);
    fireEvent.change(screen.getByLabelText("Broker order ID"), { target: { value: "order" } });
    fireEvent.change(screen.getByLabelText("Execution ID"), { target: { value: "execution" } });
    fireEvent.change(screen.getByLabelText("Fill price"), { target: { value: "100" } });
    fireEvent.change(screen.getByLabelText("Commission"), { target: { value: input } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Record verified fill" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Record verified fill" }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(capture).toHaveBeenCalledWith(7, expect.objectContaining({ commission: expected }));
  });

  it("does not offer approval or claim fill failure for a no-action review", async () => {
    vi.spyOn(api, "e2eProof").mockResolvedValue(proof);
    render(<ArgosyRunCard userId="test" compact />);
    expect(await screen.findByText(proof.headline)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve plan" })).not.toBeInTheDocument();
    expect(screen.queryByText("Approve this trade plan")).not.toBeInTheDocument();
    expect(screen.getByText("N/A")).toBeInTheDocument();
    expect(screen.queryByText(/deliberate holds/)).not.toBeInTheDocument();
  });

  it("does not let a saved no-action fallback turn into an approval card", async () => {
    vi.spyOn(api, "e2eProof").mockResolvedValue({
      ...proof, stage: "ready_to_accept",
      artifact: { ...proof.artifact!, fingerprint: "subsequent-review" },
    });
    render(<ArgosyRunCard userId="test" compact noActionFingerprint="saved-review" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The saved review has changed");
    expect(screen.queryByRole("button", { name: "Approve plan" })).not.toBeInTheDocument();
  });

  it("reports missing clocks before approval rather than promising them at materialization", async () => {
    vi.spyOn(api, "e2eProof").mockResolvedValue({
      ...proof, stage: "ready_to_accept", headline: "Validated order sheet ready for your decision",
      lines: [{
        authored: {
          symbol: "EXUS", action: "BUY", shares: 200, notional_usd: 10000,
          quantity_increment: 1, venue: "LSE", thesis: "Diversification",
          thesis_type: "diversifier", falsifier: "Exposure changes",
          catalyst: { description: "Rebalance", due_date: "2026-12-31" },
          expectation: { statement: "Diversify", due_date: "2026-12-31", success_measure: "Allocation" },
          evidence: { price_usd: 50, price_as_of: "2026-09-22T00:00:00Z", price_source: "test",
            market_cap_usd: null, incorporation_country: "IE" },
          stance_source: "rebalance", post_trade_weight_pct: 2, constraint_costs: [],
        },
        proposal: null, calibration: null,
        execution: { pending_status: null, broker: null, broker_order_id: null, filled_quantity: 0,
          fill_count: 0, vwap: null, commission_usd: 0, complete: false, manual_fill_allowed: false },
      }],
    });
    const { rerender } = render(<ArgosyRunCard userId="test" />);
    expect(await screen.findByText("Missing forecast clock")).toBeInTheDocument();
    expect(screen.queryByText("Scheduled with materialization")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeInTheDocument();
    rerender(<ArgosyRunCard userId="test" compact noActionFingerprint="test-fingerprint" />);
    expect(screen.getByRole("alert")).toHaveTextContent("The saved review has changed");
    expect(screen.queryByRole("button", { name: "Approve plan" })).not.toBeInTheDocument();
  });
});
