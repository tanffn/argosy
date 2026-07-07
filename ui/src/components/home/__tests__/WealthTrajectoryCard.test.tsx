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
  bucketQuarterly,
  buildProjection,
  formatUsdCompact,
  trendDelta,
  WealthTrajectoryCard,
} from "../WealthTrajectoryCard";

const DAY_MS = 24 * 3600 * 1000;

function isoDaysAgo(days: number): string {
  return new Date(Date.now() - days * DAY_MS).toISOString().slice(0, 10);
}

/** Three snapshots in three different quarters of the past year. */
function history(): NetWorthHistoryResponse {
  return {
    user_id: "ariel",
    points: [
      { date: isoDaysAgo(200), total_usd: 3_600_000, nvda_pct: 62.0 },
      { date: isoDaysAgo(110), total_usd: 3_800_000, nvda_pct: 60.0 },
      { date: isoDaysAgo(1), total_usd: 3_999_279, nvda_pct: 57.0 },
    ],
  };
}

// Only the fields buildProjection touches; cast keeps the fixture honest
// about being partial without replicating the whole dashboard DTO.
const DASH = {
  assumptions: { fx_usd_nis: 3.4 },
  retirement: {
    trajectory: [
      { year: 0, bear: 13_600_000, conservative: 13_800_000, typical: 14_000_000 },
      { year: 1, bear: 13_700_000, conservative: 14_100_000, typical: 14_700_000 },
      { year: 2, bear: 13_800_000, conservative: 14_400_000, typical: 15_500_000 },
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

  it("renders latest figure, book label, legends, trend pill, and history-begins note", async () => {
    netWorthHistory.mockResolvedValue(history());
    wealthDashboard.mockResolvedValue(DASH);
    render(<WealthTrajectoryCard userId="ariel" />);
    await waitFor(() =>
      expect(screen.getByTestId("wealth-latest")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("wealth-latest").textContent).toBe("$4.00M");
    // One universe, labelled.
    expect(
      screen.getByText(/Wealth trajectory — portfolio \(book\)/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/past year actual \(quarterly\)/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/1y projected \(canonical growth rates, anchored/),
    ).toBeInTheDocument();
    // History starts ~200d ago → explicit note instead of an implied
    // missing year.
    expect(screen.getByText(/history begins/)).toBeInTheDocument();
    // 3.6M → 3.8M → ~4.0M is almost exactly linear → ON TREND or a small
    // AHEAD/BEHIND — the pill must exist either way.
    expect(screen.getByTestId("wealth-trend")).toBeInTheDocument();
  });

  it("still renders actuals when the projection source fails", async () => {
    netWorthHistory.mockResolvedValue(history());
    wealthDashboard.mockRejectedValue(new Error("503"));
    render(<WealthTrajectoryCard userId="ariel" />);
    await waitFor(() =>
      expect(screen.getByTestId("wealth-latest")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/1y projected/)).not.toBeInTheDocument();
  });
});

describe("bucketQuarterly", () => {
  it("keeps the LAST point per calendar quarter within the past year", () => {
    const now = Date.now();
    const h: NetWorthHistoryResponse = {
      user_id: "ariel",
      points: [
        // Two points in the same quarter — only the later survives.
        { date: isoDaysAgo(80), total_usd: 1_000_000, nvda_pct: null },
        { date: isoDaysAgo(75), total_usd: 1_100_000, nvda_pct: null },
        // Outside the 1y window — dropped.
        { date: isoDaysAgo(400), total_usd: 900_000, nvda_pct: null },
        { date: isoDaysAgo(1), total_usd: 1_200_000, nvda_pct: null },
      ],
    };
    const rows = bucketQuarterly(h, now);
    const values = rows.map((r) => r.actual);
    expect(values).not.toContain(900_000); // windowed out
    expect(values).not.toContain(1_000_000); // superseded within its quarter
    expect(values).toContain(1_100_000);
    expect(values).toContain(1_200_000);
    const ts = rows.map((r) => r.ts);
    expect([...ts].sort((a, b) => a - b)).toEqual(ts);
  });
});

describe("buildProjection", () => {
  it("anchors at the latest actual and applies the trajectory's growth RATES", () => {
    const now = Date.now();
    const anchor = 4_000_000; // the book — NOT the ₪14M total-net-worth level
    const rows = buildProjection(DASH, now, anchor);
    expect(rows).toHaveLength(5);
    // q0 = the anchor exactly (no jump at today); q4 = anchor × y1/y0.
    expect(rows[0].projMid).toBeCloseTo(anchor, 6);
    expect(rows[0].projBand).toEqual([anchor, anchor]);
    expect(rows[4].projMid).toBeCloseTo(
      anchor * (14_700_000 / 14_000_000),
      3,
    );
    expect(rows[4].projBand![0]).toBeCloseTo(
      anchor * (13_700_000 / 13_600_000),
      3,
    );
    // Growth rates are unitless — anchored mode needs no fx.
    const dashNoFx = {
      ...DASH,
      assumptions: { fx_usd_nis: null },
    } as unknown as WealthDashboardDTO;
    expect(buildProjection(dashNoFx, now, anchor)).toHaveLength(5);
  });

  it("falls back to absolute levels ÷ fx only when there is no anchor", () => {
    const rows = buildProjection(DASH, Date.now(), null);
    expect(rows).toHaveLength(5);
    expect(rows[0].projMid).toBeCloseTo(14_000_000 / 3.4, 3);
    expect(rows[4].projMid).toBeCloseTo(14_700_000 / 3.4, 3);
  });

  it("omits the un-anchored projection when fx is unavailable (never mixed units)", () => {
    const dashNoFx = {
      ...DASH,
      assumptions: { fx_usd_nis: null },
    } as unknown as WealthDashboardDTO;
    expect(buildProjection(dashNoFx, Date.now(), null)).toHaveLength(0);
  });
});

describe("trendDelta", () => {
  const q = (n: number) => n * (365.25 / 4) * DAY_MS;

  it("positive residual when the latest point beats its own trendline", () => {
    const rows = [
      { ts: q(0), actual: 100_000 },
      { ts: q(1), actual: 200_000 },
      { ts: q(2), actual: 400_000 },
    ];
    // LSQ fit through (0,100k),(1,200k),(2,400k): fit(2)=383.3k → +16.7k.
    expect(trendDelta(rows)!).toBeCloseTo(16_666.67, 0);
  });

  it("negative residual when the latest point lags the trendline", () => {
    // Concave growth (decelerating): the last point sits BELOW the
    // straight line fitted through all three.
    const rows = [
      { ts: q(0), actual: 100_000 },
      { ts: q(1), actual: 300_000 },
      { ts: q(2), actual: 400_000 },
    ];
    // LSQ fit through (0,100k),(1,300k),(2,400k): fit(2)=416.7k → −16.7k.
    expect(trendDelta(rows)!).toBeCloseTo(-16_666.67, 0);
  });

  it("null below 3 points (a 2-point fit is vacuously exact)", () => {
    expect(
      trendDelta([
        { ts: q(0), actual: 100_000 },
        { ts: q(1), actual: 200_000 },
      ]),
    ).toBeNull();
  });
});

describe("formatUsdCompact", () => {
  it("renders clean size-proportional figures", () => {
    expect(formatUsdCompact(3_999_279)).toBe("$4.00M");
    expect(formatUsdCompact(412_345)).toBe("$412K");
  });
});
