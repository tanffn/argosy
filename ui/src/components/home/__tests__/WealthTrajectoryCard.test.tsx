import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

import type {
  NetWorthHistoryResponse,
  WealthDashboardDTO,
} from "@/lib/api";

const netWorthHistory = vi.fn();
const wealthDashboard = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      netWorthHistory: (...args: unknown[]) => netWorthHistory(...args),
      wealthDashboard: (...args: unknown[]) => wealthDashboard(...args),
    },
  };
});

import {
  buildRows,
  formatUsdCompact,
  WealthTrajectoryCard,
} from "../WealthTrajectoryCard";

const HISTORY: NetWorthHistoryResponse = {
  user_id: "ariel",
  points: [
    { date: "2026-05-01", total_usd: 3_800_000, nvda_pct: 30.0 },
    { date: "2026-06-01", total_usd: 3_999_279, nvda_pct: 28.5 },
  ],
};

// Only the fields buildRows touches; cast keeps the fixture honest about
// being partial without replicating the whole dashboard DTO.
const DASH = {
  assumptions: { fx_usd_nis: 3.4 },
  retirement: {
    trajectory: [
      { year: 0, bear: 13_600_000, conservative: 13_800_000, typical: 14_000_000 },
      { year: 1, bear: 13_700_000, conservative: 14_100_000, typical: 14_700_000 },
      { year: 2, bear: 13_800_000, conservative: 14_400_000, typical: 15_500_000 },
      { year: 3, bear: 13_900_000, conservative: 14_700_000, typical: 16_300_000 },
    ],
  },
} as unknown as WealthDashboardDTO;

beforeEach(() => {
  netWorthHistory.mockReset();
  wealthDashboard.mockReset();
});

describe("WealthTrajectoryCard", () => {
  it("shows a loading state while fetching", () => {
    netWorthHistory.mockReturnValue(new Promise(() => {}));
    wealthDashboard.mockReturnValue(new Promise(() => {}));
    render(<WealthTrajectoryCard userId="ariel" />);
    expect(screen.getByTestId("wealth-loading")).toBeInTheDocument();
  });

  it("shows the empty state when no history and no projection exist", async () => {
    netWorthHistory.mockResolvedValue({ user_id: "ariel", points: [] });
    wealthDashboard.mockRejectedValue(new Error("503"));
    render(<WealthTrajectoryCard userId="ariel" />);
    await waitFor(() =>
      expect(screen.getByTestId("wealth-empty")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("wealth-empty").textContent).toContain(
      "No snapshot history yet.",
    );
  });

  it("renders the latest actual figure and the projected legend", async () => {
    netWorthHistory.mockResolvedValue(HISTORY);
    wealthDashboard.mockResolvedValue(DASH);
    render(<WealthTrajectoryCard userId="ariel" />);
    await waitFor(() =>
      expect(screen.getByTestId("wealth-latest")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("wealth-latest").textContent).toBe("$4.00M");
    expect(screen.getByText(/projected \(canonical scenario engine\)/)).toBeInTheDocument();
    expect(screen.getByText(/last 12 months actual/)).toBeInTheDocument();
  });

  it("still renders actuals when the projection source fails", async () => {
    netWorthHistory.mockResolvedValue(HISTORY);
    wealthDashboard.mockRejectedValue(new Error("503"));
    render(<WealthTrajectoryCard userId="ariel" />);
    await waitFor(() =>
      expect(screen.getByTestId("wealth-latest")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/projected/)).not.toBeInTheDocument();
  });
});

describe("buildRows", () => {
  it("merges actual + NIS→USD-converted projection, capped at 2y, sorted", () => {
    const rows = buildRows(HISTORY, DASH);
    const actuals = rows.filter((r) => r.actual !== undefined);
    const proj = rows.filter((r) => r.projMid !== undefined);
    expect(actuals).toHaveLength(2);
    // years 0..2 only (year 3 dropped)
    expect(proj).toHaveLength(3);
    // NIS→USD conversion by the dashboard's own fx assumption
    expect(proj[0].projMid).toBeCloseTo(14_000_000 / 3.4, 3);
    expect(proj[0].projBand).toEqual([
      13_600_000 / 3.4,
      14_000_000 / 3.4,
    ]);
    // chronological
    const ts = rows.map((r) => r.ts);
    expect([...ts].sort((a, b) => a - b)).toEqual(ts);
  });

  it("omits the projection when fx is unavailable (never mixed units)", () => {
    const dashNoFx = {
      ...DASH,
      assumptions: { fx_usd_nis: null },
    } as unknown as WealthDashboardDTO;
    const rows = buildRows(HISTORY, dashNoFx);
    expect(rows.every((r) => r.projMid === undefined)).toBe(true);
  });
});

describe("formatUsdCompact", () => {
  it("renders clean size-proportional figures", () => {
    expect(formatUsdCompact(3_999_279)).toBe("$4.00M");
    expect(formatUsdCompact(412_345)).toBe("$412K");
  });
});
