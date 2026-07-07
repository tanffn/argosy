import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

import type {
  GreetingDTO,
  JobView,
  MonitorFlagDTO,
  PlanCurrentDTO,
} from "@/lib/api";

const jobsList = vi.fn();
const monitorFlags = vi.fn();
const recritique = vi.fn();
const planCurrent = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      jobs: { ...actual.api.jobs, list: (...args: unknown[]) => jobsList(...args) },
      monitorFlags: (...args: unknown[]) => monitorFlags(...args),
      recritique: (...args: unknown[]) => recritique(...args),
      planCurrent: (...args: unknown[]) => planCurrent(...args),
    },
  };
});

import {
  formatCritiqueTimestamp,
  materialChangeSinceCritique,
  nextReviewLabel,
  PlanAdherenceCard,
  severityTone,
  sortFindingsBySeverity,
  summarizeCritique,
} from "../PlanAdherenceCard";

const DAY_MS = 24 * 3600 * 1000;
const NOW = Date.now();

function plan(overrides: Partial<PlanCurrentDTO> = {}): PlanCurrentDTO {
  return {
    plan_version_id: 67,
    version_label: "x10-sleeve-draft-20260706-124710",
    raw_markdown: "",
    imported_at: new Date(NOW - 20 * DAY_MS).toISOString(),
    latest_critique_json: {
      overall_summary: "Plan holds.",
      findings: [
        { severity: "YELLOW", topic: "Data Staleness", summary: "Holdings 25d stale." },
        { severity: "RED", topic: "Cross-surface Consistency", summary: "NVDA 12% vs 8%." },
        { severity: "YELLOW", topic: "Tax Treatment", summary: "CGT unverified." },
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
  recritique.mockReset();
  planCurrent.mockReset();
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
    // Human-readable plan name — never the internal draft slug.
    const name = screen.getByTestId("adherence-plan-name").textContent!;
    expect(name).toContain("Plan v67 · x10 sleeve");
    expect(name).not.toContain("x10-sleeve-draft-20260706-124710");
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
    // Absolute generation date-time (browser-localized), then relative age.
    const expectedStamp = formatCritiqueTimestamp(
      plan().latest_critique_created_at!,
    )!;
    expect(line).toContain(`${expectedStamp} (3d ago)`);
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

  it("expands to a severity-sorted findings list (REDs first) on toggle", async () => {
    render(
      <PlanAdherenceCard userId="ariel" plan={plan()} greeting={GREETING} />,
    );
    // Collapsed by default.
    expect(screen.queryByTestId("adherence-findings")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("adherence-findings-toggle"));
    const list = screen.getByTestId("adherence-findings");
    const items = list.querySelectorAll("li");
    expect(items).toHaveLength(3);
    // The RED finding leads even though the fixture lists it second.
    expect(items[0].textContent).toContain("RED");
    expect(items[0].textContent).toContain("Cross-surface Consistency");
    expect(items[0].textContent).toContain("NVDA 12% vs 8%.");
    // Toggle closes again.
    fireEvent.click(screen.getByTestId("adherence-findings-toggle"));
    expect(screen.queryByTestId("adherence-findings")).not.toBeInTheDocument();
  });

  it("expands one finding row to its full text and collapses it again", async () => {
    render(
      <PlanAdherenceCard
        userId="ariel"
        plan={plan({
          latest_critique_json: {
            overall_summary: "s",
            findings: [
              {
                severity: "RED",
                topic: "Cross-surface Consistency",
                summary: "NVDA 12% vs 8%.",
                plan_item_ref: "IPS / Concentration — NVDA sleeve target",
                evidence: ["Table says 8.0%.", "Prose says 12.0%."],
                recommended_action: "Re-synthesize the plan.",
              },
              { severity: "GREEN", topic: "FI Math", summary: "OK." },
            ],
          },
        })}
        greeting={GREETING}
      />,
    );
    fireEvent.click(screen.getByTestId("adherence-findings-toggle"));
    // Row 0 (the RED) has detail behind a per-row expand; collapsed first.
    expect(
      screen.queryByTestId("adherence-finding-detail-0"),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("adherence-finding-toggle-0"));
    const detail = screen.getByTestId("adherence-finding-detail-0");
    expect(detail.textContent).toContain("NVDA sleeve target");
    expect(detail.textContent).toContain("Table says 8.0%.");
    expect(detail.textContent).toContain("Recommended: Re-synthesize the plan.");
    fireEvent.click(screen.getByTestId("adherence-finding-toggle-0"));
    expect(
      screen.queryByTestId("adherence-finding-detail-0"),
    ).not.toBeInTheDocument();
    // The GREEN row has no detail payload → no expand affordance.
    expect(
      screen.queryByTestId("adherence-finding-toggle-1"),
    ).not.toBeInTheDocument();
  });

  it("renders the reconcile summary line and per-row outcome tags", async () => {
    render(
      <PlanAdherenceCard
        userId="ariel"
        plan={plan({
          latest_critique_json: {
            overall_summary: "s",
            findings: [
              { severity: "RED", topic: "Ghost Row", summary: "Still stale." },
              { severity: "YELLOW", topic: "FX", summary: "Aging." },
            ],
            reconcile: {
              fixed: 1,
              escalated: 2,
              disputed_withdrawn: 1,
              summary_line:
                "reconciled: 1 fixed, 2 escalated, 1 disputed-withdrawn",
              converged: true,
              finding_status: ["escalated", null],
            },
          },
        })}
        greeting={GREETING}
      />,
    );
    expect(screen.getByTestId("adherence-reconcile").textContent).toBe(
      "reconciled: 1 fixed, 2 escalated, 1 disputed-withdrawn",
    );
    fireEvent.click(screen.getByTestId("adherence-findings-toggle"));
    // finding_status is index-aligned to the ORIGINAL order; the tagged RED
    // sorts first and carries its outcome tag.
    const list = screen.getByTestId("adherence-findings");
    const items = list.querySelectorAll("li");
    expect(items[0].textContent).toContain("Ghost Row");
    expect(items[0].textContent).toContain("escalated");
    expect(items[1].textContent).not.toContain("escalated");
  });

  it("runs a review on demand, shows the running state, and refetches", async () => {
    let resolveCritique: (v: unknown) => void = () => {};
    recritique.mockImplementation(
      () => new Promise((res) => (resolveCritique = res)),
    );
    const freshPlan = plan({
      latest_critique_json: {
        overall_summary: "All clear.",
        findings: [{ severity: "GREEN", topic: "FI Math", summary: "OK." }],
      },
      latest_critique_created_at: new Date(NOW).toISOString(),
    });
    planCurrent.mockResolvedValue(freshPlan);
    render(
      <PlanAdherenceCard userId="ariel" plan={plan()} greeting={GREETING} />,
    );
    const btn = screen.getByTestId("adherence-run-review");
    expect(btn.textContent).toBe("Run review now");
    fireEvent.click(btn);
    // Long-poll UX: disabled + honest "minutes" label while in flight.
    expect(btn.textContent).toBe("Reviewing… (minutes)");
    expect(btn).toBeDisabled();
    resolveCritique({ status: "ok", critique_id: 2, detail: "" });
    await waitFor(() =>
      expect(screen.getByTestId("adherence-run-review").textContent).toBe(
        "Run review now",
      ),
    );
    // Refetched plan overrides the prop: 1 finding, auto-expanded.
    expect(planCurrent).toHaveBeenCalledWith("ariel");
    expect(
      screen.getByTestId("adherence-critique").textContent,
    ).toContain("critique: 1 finding");
    expect(screen.getByTestId("adherence-findings").textContent).toContain(
      "FI Math",
    );
  });

  it("surfaces a review failure without losing the existing critique", async () => {
    recritique.mockRejectedValue(new Error("502 upstream"));
    render(
      <PlanAdherenceCard userId="ariel" plan={plan()} greeting={GREETING} />,
    );
    fireEvent.click(screen.getByTestId("adherence-run-review"));
    await waitFor(() =>
      expect(screen.getByTestId("adherence-review-error")).toBeInTheDocument(),
    );
    expect(
      screen.getByTestId("adherence-review-error").textContent,
    ).toContain("502 upstream");
    // Old critique line still renders.
    expect(
      screen.getByTestId("adherence-critique").textContent,
    ).toContain("critique: 3 findings");
  });

  it("deep-links to the full report on /plan#critique", async () => {
    render(
      <PlanAdherenceCard userId="ariel" plan={plan()} greeting={GREETING} />,
    );
    const link = screen.getByTestId("adherence-full-report");
    expect(link.getAttribute("href")).toBe("/plan#critique");
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
  it("sortFindingsBySeverity orders RED, then YELLOW/AMBER, then GREEN", () => {
    const sorted = sortFindingsBySeverity([
      { severity: "GREEN" },
      { severity: "YELLOW" },
      { severity: "RED" },
      { severity: "AMBER" },
    ]);
    expect(sorted.map((f) => f.severity)).toEqual([
      "RED",
      "YELLOW",
      "AMBER",
      "GREEN",
    ]);
  });

  it("severityTone maps severities to pill tones", () => {
    expect(severityTone("RED")).toBe("error");
    expect(severityTone("YELLOW")).toBe("warning");
    expect(severityTone("AMBER")).toBe("warning");
    expect(severityTone("GREEN")).toBe("success");
    expect(severityTone(undefined)).toBe("neutral");
  });

  it("summarizeCritique counts severities and flags overdue past 8 days", () => {
    const fresh = summarizeCritique(plan(), NOW)!;
    expect(fresh).toMatchObject({ total: 3, red: 1, yellow: 2, overdue: false });
    expect(fresh.ageDays).toBe(3);
    // Localized absolute timestamp of the generation time.
    expect(fresh.createdLabel).toBe(
      formatCritiqueTimestamp(plan().latest_critique_created_at!),
    );
    expect(fresh.createdLabel).toBeTruthy();
    expect(formatCritiqueTimestamp(null)).toBeNull();
    expect(formatCritiqueTimestamp("not-a-date")).toBeNull();
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
