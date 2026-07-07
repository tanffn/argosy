import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

import type {
  AllocationGlidepathResponse,
  NetWorthHistoryResponse,
} from "@/lib/api";

const netWorthHistory = vi.fn();
const planCurrentAllocationGlidepath = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      netWorthHistory: (...args: unknown[]) => netWorthHistory(...args),
      planCurrentAllocationGlidepath: (...args: unknown[]) =>
        planCurrentAllocationGlidepath(...args),
    },
  };
});

import {
  buildRows,
  DeconcentrationCard,
  dueWaypoint,
  extractWaypoints,
  findNvdaClass,
  scheduleVerdict,
} from "../DeconcentrationCard";

const NVDA_CLASS = "Strategic single-stock (NVDA)";

const HISTORY: NetWorthHistoryResponse = {
  user_id: "ariel",
  points: [
    { date: "2026-03-24", total_usd: 3_797_000, nvda_pct: 64.9 },
    { date: "2026-06-12", total_usd: 4_032_530, nvda_pct: 62.5 },
    // Uptick at the right edge — price drift between sales; still below
    // the due waypoint, so the verdict stays ON SCHEDULE.
    { date: "2026-07-06", total_usd: 3_999_279, nvda_pct: 57.0 },
    { date: "2026-07-07", total_usd: 3_993_918, nvda_pct: 57.3 },
  ],
};

/** Mirrors the live doc-backed glidepath: quarterly dated waypoints. */
function glidepath(
  overrides: Partial<AllocationGlidepathResponse> = {},
): AllocationGlidepathResponse {
  return {
    points: [
      {
        date: "2026-07-06",
        months_out: 0,
        composition_pct_by_class: { [NVDA_CLASS]: 59.5, "US broad-market core": 11.3 },
      },
      {
        date: "2026-10-06",
        months_out: 3,
        composition_pct_by_class: { [NVDA_CLASS]: 46.6, "US broad-market core": 15.6 },
      },
      {
        date: "2027-07-06",
        months_out: 12,
        composition_pct_by_class: { [NVDA_CLASS]: 8.0, "US broad-market core": 28.5 },
      },
    ] as AllocationGlidepathResponse["points"],
    collapsed_waypoints: [],
    excluded_targets: [],
    asset_classes: [NVDA_CLASS, "US broad-market core"],
    anchor_status: [],
    today: "2026-07-07",
    end_date: "2027-07-06",
    ...overrides,
  };
}

beforeEach(() => {
  netWorthHistory.mockReset();
  planCurrentAllocationGlidepath.mockReset();
});

describe("DeconcentrationCard", () => {
  it("shows a loading state while fetching", () => {
    netWorthHistory.mockReturnValue(new Promise(() => {}));
    planCurrentAllocationGlidepath.mockReturnValue(new Promise(() => {}));
    render(<DeconcentrationCard userId="ariel" />);
    expect(screen.getByTestId("decon-loading")).toBeInTheDocument();
  });

  it("shows the empty state when neither series exists", async () => {
    netWorthHistory.mockResolvedValue({ user_id: "ariel", points: [] });
    planCurrentAllocationGlidepath.mockResolvedValue(null);
    render(<DeconcentrationCard userId="ariel" />);
    await waitFor(() =>
      expect(screen.getByTestId("decon-empty")).toBeInTheDocument(),
    );
  });

  it("renders latest %, the due-waypoint line, waypoint chips, and the drift note", async () => {
    netWorthHistory.mockResolvedValue(HISTORY);
    planCurrentAllocationGlidepath.mockResolvedValue(glidepath());
    render(<DeconcentrationCard userId="ariel" />);
    await waitFor(() =>
      expect(screen.getByTestId("decon-latest")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("decon-latest").textContent).toBe("57.3%");
    // Due waypoint = the last waypoint at-or-before the latest actual
    // (2026-07-06 · 59.5%) — never the 8% end state.
    expect(screen.getByTestId("decon-due-line").textContent).toContain(
      "≤59.5%",
    );
    // 57.3 <= 59.5 + tolerance → on schedule despite the Jul 6→7 uptick.
    expect(screen.getByTestId("decon-verdict").textContent).toBe(
      "ON SCHEDULE",
    );
    // Every plan waypoint renders as a dated chip.
    const chips = screen.getByTestId("decon-waypoints");
    expect(chips.textContent).toContain("≤60%");
    expect(chips.textContent).toContain("≤47%");
    expect(chips.textContent).toContain("≤8%");
    // The uptrend-context note is explicit.
    expect(
      screen.getByText(/price drift between sales/),
    ).toBeInTheDocument();
  });

  it("flags BEHIND WAYPOINT when the latest actual exceeds the due waypoint", async () => {
    netWorthHistory.mockResolvedValue({
      user_id: "ariel",
      points: [
        { date: "2026-11-15", total_usd: 4_000_000, nvda_pct: 55.0 },
      ],
    });
    planCurrentAllocationGlidepath.mockResolvedValue(glidepath());
    render(<DeconcentrationCard userId="ariel" />);
    await waitFor(() =>
      expect(screen.getByTestId("decon-verdict")).toBeInTheDocument(),
    );
    // Due waypoint by 2026-11-15 is the Oct one (46.6%); 55 > 47.6.
    expect(screen.getByTestId("decon-verdict").textContent).toBe(
      "BEHIND WAYPOINT",
    );
  });

  it("degrades to actual-only when the plan glide is unavailable", async () => {
    netWorthHistory.mockResolvedValue(HISTORY);
    planCurrentAllocationGlidepath.mockResolvedValue(null);
    render(<DeconcentrationCard userId="ariel" />);
    await waitFor(() =>
      expect(screen.getByTestId("decon-latest")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("decon-verdict")).not.toBeInTheDocument();
    expect(screen.queryByTestId("decon-waypoints")).not.toBeInTheDocument();
    expect(screen.getByText(/plan glide unavailable/)).toBeInTheDocument();
  });
});

describe("helpers", () => {
  it("findNvdaClass matches the nvda band, falling back to individual stocks", () => {
    expect(findNvdaClass(glidepath())).toBe(NVDA_CLASS);
    expect(
      findNvdaClass(
        glidepath({ asset_classes: ["Individual Stocks", "bonds"] }),
      ),
    ).toBe("Individual Stocks");
    expect(findNvdaClass(glidepath({ asset_classes: ["bonds"] }))).toBeNull();
    expect(findNvdaClass(null)).toBeNull();
  });

  it("REGRESSION: never matches sleeves that merely reference NVDA (ex-/non-NVDA)", () => {
    // Live plan v67 ordering: 'Global quality growth (ex-NVDA-dense)'
    // sorts BEFORE 'Strategic single-stock (NVDA)'. The naive substring
    // match picked it, drawing its 4.7→11% RAMP as the NVDA glide
    // ("60% → ~5% → UP to 11%").
    const live = glidepath({
      asset_classes: [
        "Global quality growth (ex-NVDA-dense)",
        "Individual Stocks (non-NVDA, to redeploy)",
        NVDA_CLASS,
      ],
      points: [
        {
          date: "2026-07-06",
          months_out: 0,
          composition_pct_by_class: {
            "Global quality growth (ex-NVDA-dense)": 4.7,
            "Individual Stocks (non-NVDA, to redeploy)": 8.0,
            [NVDA_CLASS]: 59.5,
          },
        },
        {
          date: "2027-07-06",
          months_out: 12,
          composition_pct_by_class: {
            "Global quality growth (ex-NVDA-dense)": 11.0,
            "Individual Stocks (non-NVDA, to redeploy)": 0.0,
            [NVDA_CLASS]: 8.0,
          },
        },
      ] as AllocationGlidepathResponse["points"],
    });
    expect(findNvdaClass(live)).toBe(NVDA_CLASS);
    // The waypoint series is the true glide (59.5→8), not the 4.7→11 ramp.
    expect(extractWaypoints(live).map((w) => w.target_pct)).toEqual([
      59.5, 8.0,
    ]);
    // Even without the strategic sleeve, exclusion-qualified names and
    // the non-NVDA individual-stocks sleeve never qualify.
    expect(
      findNvdaClass(
        glidepath({
          asset_classes: [
            "Global quality growth (ex-NVDA-dense)",
            "Individual Stocks (non-NVDA, to redeploy)",
          ],
        }),
      ),
    ).toBeNull();
  });

  it("buildRows windows actuals to 1 year past; the glide runs to its end", () => {
    const now = new Date("2026-07-07").getTime();
    const wps = extractWaypoints(glidepath());
    const withOld: NetWorthHistoryResponse = {
      user_id: "ariel",
      points: [
        { date: "2025-05-01", total_usd: 3_000_000, nvda_pct: 70.0 }, // >1y old
        ...HISTORY.points,
      ],
    };
    const rows = buildRows(withOld, wps, now);
    expect(rows.some((r) => r.actual === 70.0)).toBe(false);
    expect(rows.some((r) => r.actual === 64.9)).toBe(true);
    // Glide end (2027-07-06) survives untrimmed.
    expect(rows.some((r) => r.glide === 8.0)).toBe(true);
  });

  it("extractWaypoints returns the NVDA band's dated waypoints in order", () => {
    const wps = extractWaypoints(glidepath());
    expect(wps.map((w) => w.date)).toEqual([
      "2026-07-06",
      "2026-10-06",
      "2027-07-06",
    ]);
    expect(wps.map((w) => w.target_pct)).toEqual([59.5, 46.6, 8.0]);
    expect(extractWaypoints(null)).toEqual([]);
  });

  it("dueWaypoint picks the last waypoint at-or-before the latest actual", () => {
    const wps = extractWaypoints(glidepath());
    const rows = buildRows(HISTORY, wps);
    expect(dueWaypoint(rows, wps)?.date).toBe("2026-07-06");
    // No actuals → null.
    expect(dueWaypoint(buildRows(null, wps), wps)).toBeNull();
  });

  it("scheduleVerdict compares against the due waypoint, not the end state", () => {
    const wps = extractWaypoints(glidepath());
    expect(scheduleVerdict(buildRows(HISTORY, wps), wps)).toBe("on");
    const behind: NetWorthHistoryResponse = {
      user_id: "ariel",
      points: [{ date: "2026-10-20", total_usd: 4_000_000, nvda_pct: 52.0 }],
    };
    expect(scheduleVerdict(buildRows(behind, wps), wps)).toBe("behind");
    expect(scheduleVerdict(buildRows(HISTORY, []), [])).toBeNull();
  });
});
