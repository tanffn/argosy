import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

import type {
  GreetingDTO,
  JobView,
  MonitorFlagDTO,
  PlanCurrentDTO,
} from "@/lib/api";

const jobsList = vi.fn();
const monitorFlags = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      jobs: { ...actual.api.jobs, list: (...args: unknown[]) => jobsList(...args) },
      monitorFlags: (...args: unknown[]) => monitorFlags(...args),
    },
  };
});

import {
  materialChangeSinceCritique,
  nextReviewLabel,
  PlanAdherenceCard,
  summarizeCritique,
} from "../PlanAdherenceCard";

const DAY_MS = 24 * 3600 * 1000;
const NOW = Date.now();

function plan(overrides: Partial<PlanCurrentDTO> = {}): PlanCurrentDTO {
  return {
    plan_version_id: 67,
    version_label: "v67",
    raw_markdown: "",
    imported_at: new Date(NOW - 20 * DAY_MS).toISOString(),
    latest_critique_json: {
      overall_summary: "Plan holds.",
      findings: [
        { severity: "RED" },
        { severity: "YELLOW" },
        { severity: "YELLOW" },
      ],
    },
    latest_critique_created_at: new Date(NOW - 3 * DAY_MS).toISOString(),
    ...overrides,
  };
}

const GREETING = {
  greeting_name: "Ariel",
  book: {
    total_usd: 4_000_000,
    on_plan: false,
    on_plan_note: "transition in progress — biggest gap: Individual Stocks",
    fi_line: "FI track: 2028 (age 46)",
  },
  needs_you: [],
  watching: [],
  quiet: true,
  next_review_local: "17:00",
} as GreetingDTO;

function weeklyReviewJob(overrides: Partial<JobView> = {}): JobView {
  return {
    metadata: {
      name: "weekly_review",
      schedule_cron: "0 18 * * SUN",
      schedule_human: "cron 0 18 * * SUN (Asia/Jerusalem)",
      source_kind: "maintenance",
      description: "",
      long_running: false,
    },
    last_run_at: new Date(NOW - 1 * DAY_MS).toISOString(),
    last_run_status: "ok",
    last_run_error: null,
    next_run_at: null,
    currently_running_run_id: null,
    health: "healthy",
    ...overrides,
  } as JobView;
}

function flag(overrides: Partial<MonitorFlagDTO> = {}): MonitorFlagDTO {
  return {
    id: 90,
    kind: "state_observer_allocation_observation",
    severity: "warning",
    payload: {},
    surfaced_at: new Date(NOW - 1 * DAY_MS).toISOString(),
    expires_at: null,
    acknowledged_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  jobsList.mockReset();
  monitorFlags.mockReset();
  jobsList.mockResolvedValue({ jobs: [weeklyReviewJob()] });
  monitorFlags.mockResolvedValue([]);
});

describe("PlanAdherenceCard", () => {
  it("leads with the greeting's canonical on-plan status + note", async () => {
    render(
      <PlanAdherenceCard userId="ariel" plan={plan()} greeting={GREETING} />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("adherence-status")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("adherence-status").textContent).toBe(
      "IN TRANSITION",
    );
    expect(screen.getByTestId("adherence-note").textContent).toContain(
      "biggest gap",
    );
  });

  it("shows the critique verdict with its age and the next auto-review", async () => {
    render(
      <PlanAdherenceCard userId="ariel" plan={plan()} greeting={GREETING} />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId("adherence-critique").textContent,
      ).toContain("next auto-review"),
    );
    const line = screen.getByTestId("adherence-critique").textContent!;
    expect(line).toContain("critique: 3 findings");
    expect(line).toContain("1 RED");
    expect(line).toContain("2 YELLOW");
    expect(line).toContain("3d ago");
    // Cron "0 18 * * SUN" → Sun 18:00.
    expect(line).toContain("Sun 18:00");
    expect(screen.queryByTestId("adherence-overdue")).not.toBeInTheDocument();
  });

  it("shows an honest refresh-overdue pill when the critique is stale", async () => {
    render(
      <PlanAdherenceCard
        userId="ariel"
        plan={plan({
          latest_critique_created_at: new Date(NOW - 12 * DAY_MS).toISOString(),
        })}
        greeting={GREETING}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("adherence-overdue")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("adherence-overdue").textContent).toBe(
      "refresh overdue",
    );
  });

  it("surfaces a material allocation change observed since the last critique", async () => {
    monitorFlags.mockResolvedValue([flag()]);
    render(
      <PlanAdherenceCard userId="ariel" plan={plan()} greeting={GREETING} />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId("adherence-material-change"),
      ).toBeInTheDocument(),
    );
  });

  it("degrades honestly with no critique on file", async () => {
    render(
      <PlanAdherenceCard
        userId="ariel"
        plan={plan({
          latest_critique_json: null,
          latest_critique_created_at: null,
        })}
        greeting={null}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId("adherence-critique").textContent,
      ).toContain("no critique on file yet"),
    );
    expect(screen.queryByTestId("adherence-status")).not.toBeInTheDocument();
  });
});

describe("helpers", () => {
  it("summarizeCritique counts severities and flags overdue past 8 days", () => {
    const fresh = summarizeCritique(plan(), NOW)!;
    expect(fresh).toMatchObject({ total: 3, red: 1, yellow: 2, overdue: false });
    expect(fresh.ageDays).toBe(3);
    const stale = summarizeCritique(
      plan({
        latest_critique_created_at: new Date(NOW - 9 * DAY_MS).toISOString(),
      }),
      NOW,
    )!;
    expect(stale.overdue).toBe(true);
    expect(summarizeCritique(plan({ latest_critique_json: null }), NOW)).toBeNull();
  });

  it("nextReviewLabel prefers next_run_at, then reads the cron", () => {
    // next_run_at wins.
    const withNext = weeklyReviewJob({
      next_run_at: "2026-07-12T18:00:00",
    });
    expect(nextReviewLabel(withNext)).toMatch(/18:00/);
    // Cron fallback.
    expect(nextReviewLabel(weeklyReviewJob())).toBe("Sun 18:00");
    expect(nextReviewLabel(null)).toBeNull();
  });

  it("materialChangeSinceCritique requires an active flag newer than the critique", () => {
    const p = plan();
    // Newer than the 3d-old critique → true.
    expect(materialChangeSinceCritique([flag()], p, NOW)).toBe(true);
    // Older than the critique → false.
    expect(
      materialChangeSinceCritique(
        [flag({ surfaced_at: new Date(NOW - 5 * DAY_MS).toISOString() })],
        p,
        NOW,
      ),
    ).toBe(false);
    // Acknowledged → false.
    expect(
      materialChangeSinceCritique(
        [flag({ acknowledged_at: new Date(NOW).toISOString() })],
        p,
        NOW,
      ),
    ).toBe(false);
    // Non-material kind → false.
    expect(
      materialChangeSinceCritique(
        [flag({ kind: "state_observer_fx_observation" })],
        p,
        NOW,
      ),
    ).toBe(false);
    // No critique on file → any active material flag counts.
    expect(
      materialChangeSinceCritique(
        [flag()],
        plan({ latest_critique_created_at: null }),
        NOW,
      ),
    ).toBe(true);
  });
});
