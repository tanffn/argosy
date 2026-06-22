// ui/src/components/proposals/__tests__/WithholdingCheckCard.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { WithholdingCheckResponse } from "@/lib/api";

const RECONCILED: WithholdingCheckResponse = {
  has_verdict: true,
  period_year: 2026,
  period_month: 4,
  ingested_at: "2026-05-01T00:00:00Z",
  status: "reconciled",
  verdict: {
    status: "reconciled",
    period: 2026,
    equity_ordinary_base: 60679,
    equity_capital_base: 549467,
    actual_tax_withheld: 167707,
    expected_at_wire_rate: 167706.25,
    reconc_residual: 0.75,
    conservative_liability: 175082,
    potential_filing_topup: 7375,
    effective_rate_pct: 27.5,
    summary: "Your payslip reconciles ₪167,707 of §102 equity tax YTD.",
    confidence: "high",
    caveats: ["scope caveat one", "rate caveat two"],
  },
};

const NO_DATA: WithholdingCheckResponse = {
  has_verdict: false,
  period_year: null,
  period_month: null,
  ingested_at: null,
  status: "no_data",
  verdict: null,
};

function mockApi(resp: WithholdingCheckResponse) {
  vi.doMock("@/lib/api", async (importOriginal) => {
    const actual = await importOriginal<typeof import("@/lib/api")>();
    return {
      ...actual,
      api: {
        ...actual.api,
        taxWithholdingCheck: vi.fn().mockResolvedValue(resp),
      },
    };
  });
}

describe("WithholdingCheckCard", () => {
  it("renders the reconciled verdict, status pill, ₪ numbers and top-up", async () => {
    vi.resetModules();
    mockApi(RECONCILED);
    const { WithholdingCheckCard: Card } = await import("../WithholdingCheckCard");
    render(<Card userId="ariel" />);

    await waitFor(() =>
      expect(screen.getByText(/Reconciled/)).toBeInTheDocument(),
    );
    expect(screen.getByText(/reconciles ₪167,707/)).toBeInTheDocument();
    expect(screen.getByText("₪167,707")).toBeInTheDocument();
    expect(screen.getByText(/Set aside ~₪7,375/)).toBeInTheDocument();
    expect(screen.getByText(/Apr 2026/)).toBeInTheDocument();
  });

  it("renders an honest empty state when no payslip is ingested", async () => {
    vi.resetModules();
    mockApi(NO_DATA);
    const { WithholdingCheckCard: Card } = await import("../WithholdingCheckCard");
    render(<Card userId="ariel" />);

    await waitFor(() =>
      expect(screen.getByText(/No payslip yet/)).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/cannot verify the §102 RSU withholding/),
    ).toBeInTheDocument();
  });
});

// Keep the static type import referenced so the file type-checks cleanly even
// if a future refactor drops a usage above.
export type _Ref = WithholdingCheckResponse;
