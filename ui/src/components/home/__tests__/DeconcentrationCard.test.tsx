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
  findNvdaClass,
  scheduleVerdict,
} from "../DeconcentrationCard";

const HISTORY: NetWorthHistoryResponse = {
  user_id: "ariel",
  points: [
    { date: "2026-05-01", total_usd: 3_800_000, nvda_pct: 31.2 },
    { date: "2026-06-01", total_usd: 3_999_279, nvda_pct: 28.4 },
  ],
};

function glidepath(
  overrides: Partial<AllocationGlidepathResponse> = {},
): AllocationGlidepathResponse {
  return {
    points: [
      {
        date: "2026-06-01",
        months_out: 0,
        composition_pct_by_class: { nvda: 28.0, "global equity": 40.0 },
      },
      {
        date: "2027-06-01",
        months_out: 12,
        composition_pct_by_class: { nvda: 20.0, "global equity": 46.0 },
      },
    ] as AllocationGlidepathResponse["points"],
    collapsed_waypoints: [],
    excluded_targets: [],
    asset_classes: ["nvda", "global equity"],
    anchor_status: [],
    today: "2026-06-01",
    end_date: "2027-06-01",
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

  it("renders latest actual % and an ON SCHEDULE verdict vs the glide", async () => {
    netWorthHistory.mockResolvedValue(HISTORY);
    planCurrentAllocationGlidepath.mockResolvedValue(glidepath());
    render(<DeconcentrationCard userId="ariel" />);
    await waitFor(() =>
      expect(screen.getByTestId("decon-latest")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("decon-latest").textContent).toBe("28.4%");
    // 28.4 <= 28.0 + 1pp tolerance → on schedule
    expect(screen.getByTestId("decon-verdict").textContent).toBe(
      "ON SCHEDULE",
    );
    expect(screen.getByText(/plan glide waypoints/)).toBeInTheDocument();
  });

  it("degrades to actual-only when the plan glide is unavailable", async () => {
    netWorthHistory.mockResolvedValue(HISTORY);
    planCurrentAllocationGlidepath.mockResolvedValue(null);
    render(<DeconcentrationCard userId="ariel" />);
    await waitFor(() =>
      expect(screen.getByTestId("decon-latest")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("decon-verdict")).not.toBeInTheDocument();
    expect(screen.getByText(/plan glide unavailable/)).toBeInTheDocument();
  });
});

describe("helpers", () => {
  it("findNvdaClass matches the nvda band, falling back to individual stocks", () => {
    expect(findNvdaClass(glidepath())).toBe("nvda");
    expect(
      findNvdaClass(
        glidepath({ asset_classes: ["Individual Stocks", "bonds"] }),
      ),
    ).toBe("Individual Stocks");
    expect(findNvdaClass(glidepath({ asset_classes: ["bonds"] }))).toBeNull();
    expect(findNvdaClass(null)).toBeNull();
  });

  it("scheduleVerdict flags BEHIND when latest actual exceeds the due glide", () => {
    const behindHistory: NetWorthHistoryResponse = {
      user_id: "ariel",
      points: [{ date: "2026-06-15", total_usd: 4_000_000, nvda_pct: 33.0 }],
    };
    expect(scheduleVerdict(buildRows(behindHistory, glidepath()))).toBe(
      "behind",
    );
    expect(scheduleVerdict(buildRows(HISTORY, glidepath()))).toBe("on");
    expect(scheduleVerdict(buildRows(HISTORY, null))).toBeNull();
  });
});
