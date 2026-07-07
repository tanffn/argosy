import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { NvdaPaceDTO } from "@/lib/api";

import { NvdaPaceTile, nvdaOnPace } from "../NvdaPaceTile";

// Fixed "today": 2026-07-07 local midnight — day 2 of a plan started Jul 6.
const NOW = new Date("2026-07-07T00:00:00").getTime();

// Tax-year framed glide payload (the live shape after the quota rework):
// quota ~4,810 sh for calendar 2026; 1,600 already sold (pre-plan sales
// count); next glide checkpoint Oct 6 at ≤47% (~2,470 sh to go).
const QUOTA_PACE: NvdaPaceDTO = {
  shares_sold_ytd: 1600,
  target_shares_ytd: 1627,
  delta_shares: -27,
  on_track: true,
  status: "on",
  basis: "glide",
  plan_start: "2026-07-06",
  sold_calendar_ytd: 1600,
  tax_year: 2026,
  year_target_shares: 4810,
  next_waypoint_date: "2026-10-06",
  next_waypoint_weight_pct: 47.0,
  shares_to_sell_by_waypoint: 2470,
  sold_since_plan_start: 0,
};

// Legacy glide payload (no tax-year quota fields) — pre-rework backend.
const LEGACY_GLIDE_PACE: NvdaPaceDTO = {
  shares_sold_ytd: 0,
  target_shares_ytd: 27,
  delta_shares: -27,
  on_track: true,
  basis: "glide",
  plan_start: "2026-07-06",
  sold_calendar_ytd: 1600,
};

const HORIZON_PACE: NvdaPaceDTO = {
  shares_sold_ytd: 1600,
  target_shares_ytd: 3000,
  delta_shares: -1400,
  on_track: true,
  basis: "horizon",
  plan_start: null,
  sold_calendar_ytd: 1600,
};

describe("NvdaPaceTile", () => {
  it("headlines the tax-year quota on basis=glide — calendar sold is first-class", () => {
    render(<NvdaPaceTile pace={QUOTA_PACE} now={NOW} />);
    // Headline: the year quota + the calendar-year sold count.
    expect(
      screen.getByText("2026 target: sell ~4,810 sh by Dec 31 · 1,600 sold"),
    ).toBeInTheDocument();
    // Right side: quota progress vs time through the tax year.
    expect(
      screen.getByText(/33% of quota · \d+% of year elapsed/),
    ).toBeInTheDocument();
    // Secondary line: the next glide checkpoint.
    expect(
      screen.getByText("Next waypoint Oct 6: ≤47% — sell ~2,470 sh by then"),
    ).toBeInTheDocument();
    // The daily pro-rata vocabulary must be gone.
    expect(screen.queryByText(/day \d+ of the plan year/)).toBeNull();
    expect(screen.getByText("ON PACE")).toBeInTheDocument();
  });

  it("renders BEHIND PACE from the backend's quota-banded status", () => {
    render(
      <NvdaPaceTile
        pace={{
          ...QUOTA_PACE,
          status: "behind",
          on_track: false,
          delta_shares: -900,
        }}
        now={NOW}
      />,
    );
    expect(screen.getByText("BEHIND PACE")).toBeInTheDocument();
  });

  it("renders AHEAD OF PACE when the backend says ahead", () => {
    render(
      <NvdaPaceTile
        pace={{ ...QUOTA_PACE, status: "ahead", delta_shares: 600 }}
        now={NOW}
      />,
    );
    expect(screen.getByText("AHEAD OF PACE")).toBeInTheDocument();
  });

  it("omits the waypoint line when no dated checkpoint remains", () => {
    render(
      <NvdaPaceTile
        pace={{
          ...QUOTA_PACE,
          next_waypoint_date: null,
          next_waypoint_weight_pct: null,
          shares_to_sell_by_waypoint: null,
        }}
        now={NOW}
      />,
    );
    expect(screen.queryByText(/Next waypoint/)).toBeNull();
    // The quota headline still renders.
    expect(
      screen.getByText("2026 target: sell ~4,810 sh by Dec 31 · 1,600 sold"),
    ).toBeInTheDocument();
  });

  it("falls back to plan-relative copy on legacy glide payloads", () => {
    const { container } = render(
      <NvdaPaceTile pace={LEGACY_GLIDE_PACE} now={NOW} />,
    );
    expect(
      screen.getByText("0 / 27 shares · day 2 of the plan year"),
    ).toBeInTheDocument();
    expect(screen.getByText("plan started Jul 6 · on pace")).toBeInTheDocument();
    expect(screen.getByText("1,600 sold in 2026 pre-plan")).toBeInTheDocument();
    expect(container.textContent).not.toContain("YTD");
    expect(screen.getByText("ON PACE")).toBeInTheDocument();
  });

  it("keeps calendar labels on basis=horizon", () => {
    render(<NvdaPaceTile pace={HORIZON_PACE} now={NOW} />);
    expect(
      screen.getByText("1,600 / 3,000 shares sold YTD (plan target)"),
    ).toBeInTheDocument();
    // "53.3% of plan target · 51% of year elapsed" (Jul 7 2026).
    expect(
      screen.getByText(/53\.3% of plan target · \d+% of year elapsed/),
    ).toBeInTheDocument();
    // No plan-relative copy on the calendar basis.
    expect(screen.queryByText(/day \d+ of the plan year/)).toBeNull();
    expect(screen.queryByText(/pre-plan/)).toBeNull();
  });

  it("treats a missing basis (legacy payload) as calendar", () => {
    const legacy: NvdaPaceDTO = {
      shares_sold_ytd: 10,
      target_shares_ytd: 100,
      delta_shares: -90,
      on_track: false,
    };
    render(<NvdaPaceTile pace={legacy} now={NOW} />);
    expect(
      screen.getByText("10 / 100 shares sold YTD (plan target)"),
    ).toBeInTheDocument();
    expect(screen.getByText("BEHIND PACE")).toBeInTheDocument();
  });

  it("renders nothing without a target denominator", () => {
    const { container } = render(
      <NvdaPaceTile pace={{ ...QUOTA_PACE, target_shares_ytd: 0 }} now={NOW} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe("nvdaOnPace", () => {
  it("trusts the backend status when present", () => {
    expect(nvdaOnPace({ ...QUOTA_PACE, status: "behind" })).toBe(false);
    expect(nvdaOnPace({ ...QUOTA_PACE, status: "on" })).toBe(true);
    expect(nvdaOnPace({ ...QUOTA_PACE, status: "ahead" })).toBe(true);
  });

  it("legacy payloads: prefers on_track, tolerates <20% under-target", () => {
    const base = { delta_shares: 0, on_track: false };
    expect(
      nvdaOnPace({ ...base, shares_sold_ytd: 81, target_shares_ytd: 100 }),
    ).toBe(true); // 19% under
    expect(
      nvdaOnPace({ ...base, shares_sold_ytd: 80, target_shares_ytd: 100 }),
    ).toBe(false); // 20% under
    expect(
      nvdaOnPace({
        ...base,
        shares_sold_ytd: 0,
        target_shares_ytd: 27,
        on_track: true,
      }),
    ).toBe(true); // backend owns the schedule semantics
  });
});
