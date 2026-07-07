import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { NvdaPaceDTO } from "@/lib/api";

import { NvdaPaceTile, nvdaOnPace } from "../NvdaPaceTile";

// Fixed "today": 2026-07-07 local midnight — day 2 of a plan started Jul 6.
const NOW = new Date("2026-07-07T00:00:00").getTime();

const GLIDE_PACE: NvdaPaceDTO = {
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
  it("labels plan-relative on basis=glide — never YTD / % of year elapsed", () => {
    const { container } = render(<NvdaPaceTile pace={GLIDE_PACE} now={NOW} />);
    // Plan-relative main line: "0 / 27 shares · day 2 of the plan year".
    expect(
      screen.getByText("0 / 27 shares · day 2 of the plan year"),
    ).toBeInTheDocument();
    // Right side: plan start + pace word.
    expect(screen.getByText("plan started Jul 6 · on pace")).toBeInTheDocument();
    // Muted calendar context: pre-plan sales via the API payload.
    expect(screen.getByText("1,600 sold in 2026 pre-plan")).toBeInTheDocument();
    // The calendar-basis vocabulary must be absent.
    expect(container.textContent).not.toContain("YTD");
    expect(container.textContent).not.toContain("of year elapsed");
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
      <NvdaPaceTile
        pace={{ ...GLIDE_PACE, target_shares_ytd: 0 }}
        now={NOW}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("omits the pre-plan context line when calendar equals the plan window", () => {
    render(
      <NvdaPaceTile
        pace={{ ...GLIDE_PACE, shares_sold_ytd: 5, sold_calendar_ytd: 5 }}
        now={NOW}
      />,
    );
    expect(screen.queryByText(/pre-plan/)).toBeNull();
  });
});

describe("nvdaOnPace", () => {
  it("prefers on_track, tolerates <20% under-target, flags >=20%", () => {
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
