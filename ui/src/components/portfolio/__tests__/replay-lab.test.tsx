import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { HistoricalReplayDTO } from "@/lib/api";
import { HistoricalReplay } from "../discovery-card";

describe("Replay lab", () => {
  it("shows the lab even without a legacy scored run and labels unknown outcomes", () => {
    const data: HistoricalReplayDTO = {
      status: "not_run", separate_from_forward_live: true, message: "No legacy run", cases: [],
      coverage: { packets: 0, immutable_receipts: 0, replay_ready: 0, missing_receipt: 0, temporal_disqualified: 0 },
      lab: { status: "incomplete", run_id: "example", controls_passed: false,
        limitations: ["Not proof of investment alpha"],
        cases: [{ case_id: "masked", synthetic: false, action: "sell", confidence: "HIGH",
          qualified: false, exclusion_reason: "Missing review", market_horizons: [
            { months: 6, status: "missing_price_data", subject_return_pct: null,
              benchmark_return_pct: null, excess_return_pp: null },
          ] }],
      },
    };
    render(<HistoricalReplay data={data} />);
    expect(screen.getByText(/historical interpretation blocked/)).toBeInTheDocument();
    expect(screen.getByText(/not after-tax trade returns/)).toBeInTheDocument();
    expect(screen.getByText("6 months: missing price data")).toBeInTheDocument();
    expect(screen.getByText("Missing review")).toBeInTheDocument();
  });
});
