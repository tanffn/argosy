"use client";

/**
 * PlanAdherenceCard — the reworked PLAN panel. Leads with a computed
 * STATUS from live deterministic sources instead of a bare critique
 * dump:
 *
 *  1. STATUS — the greeting's canonical on_plan computation (allocation
 *     breakdown vs TargetAllocationDoc bands + concentration cap state,
 *     assembled server-side in /api/home/greeting). The greeting DTO is
 *     passed down from the page (FMGreetingCard already fetched it) so
 *     the logic is not duplicated client-side and not fetched twice.
 *  2. CRITIQUE — the latest plan-critique verdict WITH ITS AGE, plus
 *     the next auto-review fire from /api/jobs (the weekly_review loop
 *     runs the critique weekly). If the critique is older than the
 *     weekly cadence promises (+1d grace), an honest "refresh overdue"
 *     pill appears.
 *  3. MATERIAL CHANGE — when the state observer has an active
 *     allocation/concentration observation flag newer than the last
 *     critique, the panel says so: the change is flagged for the next
 *     auto-review. (Surfacing only — the material-change trigger itself
 *     belongs to the state observer, not the UI.)
 */

import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type GreetingDTO,
  type JobView,
  type MonitorFlagDTO,
  type PlanCurrentDTO,
} from "@/lib/api";
import { formatPlanLabel } from "@/lib/plan-label";

/** weekly cadence + 1 day of grace before "refresh overdue". */
const CRITIQUE_OVERDUE_MS = 8 * 24 * 3600 * 1000;

/** Monitor-flag kinds that mean "the allocation materially moved". */
const MATERIAL_FLAG_KINDS = [
  "state_observer_allocation_observation",
  "state_observer_concentration_observation",
  "allocation_drift",
];

export interface CritiqueFinding {
  severity?: string;
  topic?: string;
  summary?: string;
  plan_item_ref?: string;
  evidence?: string[];
  recommended_action?: string | null;
  /** Reconcile-loop outcome tag for this row (attached client-side from
   * the critique JSON's reconcile.finding_status, index-aligned). */
  outcome?: string | null;
}

/** Reconcile-loop payload embedded in the re-verify critique JSON. */
export interface CritiqueReconcile {
  fixed?: number;
  escalated?: number;
  disputed_withdrawn?: number;
  summary_line?: string;
  converged?: boolean;
  /** Index-aligned to the critique's findings array. */
  finding_status?: (string | null)[];
}

interface CritiqueShape {
  overall_summary?: string;
  findings?: CritiqueFinding[];
  reconcile?: CritiqueReconcile;
}

/** REDs first, then YELLOW/AMBER, then GREEN, stable within a band. */
const SEVERITY_ORDER: Record<string, number> = {
  RED: 0,
  AMBER: 1,
  YELLOW: 1,
  GREEN: 2,
};

export function sortFindingsBySeverity(
  findings: CritiqueFinding[],
): CritiqueFinding[] {
  return [...findings].sort(
    (a, b) =>
      (SEVERITY_ORDER[a.severity ?? ""] ?? 3) -
      (SEVERITY_ORDER[b.severity ?? ""] ?? 3),
  );
}

export function severityTone(
  severity: string | undefined,
): "success" | "warning" | "error" | "neutral" {
  switch (severity) {
    case "RED":
      return "error";
    case "YELLOW":
    case "AMBER":
      return "warning";
    case "GREEN":
      return "success";
    default:
      return "neutral";
  }
}

export interface CritiqueLine {
  total: number;
  red: number;
  yellow: number;
  ageDays: number | null;
  /** Localized generation date-time, e.g. "Jul 7, 21:34" (browser locale). */
  createdLabel: string | null;
  overdue: boolean;
}

/** "Jul 7, 21:34" in the browser's locale/timezone. */
export function formatCritiqueTimestamp(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function summarizeCritique(
  plan: PlanCurrentDTO | null,
  now: number,
): CritiqueLine | null {
  const critique = (plan?.latest_critique_json ?? null) as CritiqueShape | null;
  if (!critique) return null;
  const findings = critique.findings ?? [];
  const red = findings.filter((f) => f.severity === "RED").length;
  const yellow = findings.filter((f) => f.severity === "YELLOW").length;
  let ageDays: number | null = null;
  let overdue = false;
  const createdLabel = formatCritiqueTimestamp(
    plan?.latest_critique_created_at ?? null,
  );
  if (plan?.latest_critique_created_at) {
    const t = new Date(plan.latest_critique_created_at).getTime();
    if (!Number.isNaN(t)) {
      ageDays = Math.max(0, Math.floor((now - t) / (24 * 3600 * 1000)));
      overdue = now - t > CRITIQUE_OVERDUE_MS;
    }
  }
  return { total: findings.length, red, yellow, ageDays, createdLabel, overdue };
}

/**
 * Human "next auto-review" from the weekly_review job: prefer the
 * scheduler's own next_run_at; fall back to reading the cron spec
 * ("0 18 * * SUN" → "Sun 18:00"), then to schedule_human verbatim.
 */
export function nextReviewLabel(job: JobView | null): string | null {
  if (!job) return null;
  if (job.next_run_at) {
    const d = new Date(job.next_run_at);
    if (!Number.isNaN(d.getTime())) {
      return d.toLocaleString([], {
        weekday: "short",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
    }
  }
  if (job.schedule_cron) {
    const m = job.schedule_cron
      .trim()
      .match(/^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+(\w{3})$/i);
    if (m) {
      const [, min, hour, dow] = m;
      const day = dow.charAt(0).toUpperCase() + dow.slice(1).toLowerCase();
      return `${day} ${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;
    }
  }
  return job.schedule_human || null;
}

/**
 * True when an ACTIVE (unacknowledged, unexpired) allocation /
 * concentration observation was surfaced AFTER the latest critique —
 * i.e. the book materially moved and the critique hasn't seen it yet.
 */
export function materialChangeSinceCritique(
  flags: MonitorFlagDTO[],
  plan: PlanCurrentDTO | null,
  now: number,
): boolean {
  const critiqueTs = plan?.latest_critique_created_at
    ? new Date(plan.latest_critique_created_at).getTime()
    : null;
  return flags.some((f) => {
    if (!MATERIAL_FLAG_KINDS.includes(f.kind)) return false;
    if (f.acknowledged_at) return false;
    if (f.expires_at && new Date(f.expires_at).getTime() < now) return false;
    const surfaced = new Date(f.surfaced_at).getTime();
    if (Number.isNaN(surfaced)) return false;
    return critiqueTs === null || Number.isNaN(critiqueTs)
      ? true
      : surfaced > critiqueTs;
  });
}

interface Props {
  userId: string;
  plan: PlanCurrentDTO | null;
  /** The greeting the page already fetched (via FMGreetingCard onLoaded). */
  greeting: GreetingDTO | null;
}

export function PlanAdherenceCard({ userId, plan: planProp, greeting }: Props) {
  const [weeklyReview, setWeeklyReview] = useState<JobView | null>(null);
  const [flags, setFlags] = useState<MonitorFlagDTO[]>([]);
  // Clock stamped from the effect so render stays pure.
  const [nowTs, setNowTs] = useState<number | null>(null);
  // Findings drill-in (collapsed by default — the one-line summary stays
  // the primary surface; expanding shows the severity-sorted list).
  const [expanded, setExpanded] = useState(false);
  // "Run review now" — mirrors DeployCashCard's fleet-review UX: the
  // POST is a synchronous Opus call that takes minutes, so the button
  // flips to a disabled "running… (minutes)" label and we refetch
  // /api/plan/current when it returns. The fresh plan (with the new
  // critique) overrides the page-supplied prop until the next reload.
  const [reviewRunning, setReviewRunning] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [freshPlan, setFreshPlan] = useState<PlanCurrentDTO | null>(null);

  const plan = freshPlan ?? planProp;

  const runReviewNow = async () => {
    setReviewRunning(true);
    setReviewError(null);
    try {
      await api.recritique(userId);
      const updated = await api.planCurrent(userId);
      setFreshPlan(updated);
      setNowTs(Date.now());
      setExpanded(true);
    } catch (err) {
      setReviewError(err instanceof Error ? err.message : String(err));
    } finally {
      setReviewRunning(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([api.jobs.list(), api.monitorFlags(userId)]).then(
      ([jobsRes, flagsRes]) => {
        if (cancelled) return;
        if (jobsRes.status === "fulfilled") {
          setWeeklyReview(
            jobsRes.value.jobs.find((j) => j.name === "weekly_review") ?? null,
          );
        }
        if (flagsRes.status === "fulfilled") setFlags(flagsRes.value);
        setNowTs(Date.now());
      },
    );
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const critique = useMemo(
    () => summarizeCritique(plan, nowTs ?? 0),
    [plan, nowTs],
  );
  const critiqueShape = (plan?.latest_critique_json ?? null) as
    | CritiqueShape
    | null;
  const reconcile = critiqueShape?.reconcile ?? null;
  const findings = useMemo(() => {
    const raw = critiqueShape?.findings ?? [];
    // Attach the reconcile outcome tag BEFORE sorting — finding_status is
    // index-aligned to the critique's original findings order.
    const status = critiqueShape?.reconcile?.finding_status ?? [];
    return sortFindingsBySeverity(
      raw.map((f, i) => ({ ...f, outcome: status[i] ?? null })),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan]);
  // Per-row drill-in: which finding rows show their full text. Keyed by
  // the critique's timestamp so a fresh critique renders collapsed without
  // needing a reset effect.
  const critiqueKey = plan?.latest_critique_created_at ?? null;
  const [openRowsState, setOpenRowsState] = useState<{
    key: string | null;
    rows: Set<number>;
  }>({ key: null, rows: new Set() });
  const openRows =
    openRowsState.key === critiqueKey ? openRowsState.rows : new Set<number>();
  const toggleRow = (i: number) =>
    setOpenRowsState((prev) => {
      const rows =
        prev.key === critiqueKey ? new Set(prev.rows) : new Set<number>();
      if (rows.has(i)) rows.delete(i);
      else rows.add(i);
      return { key: critiqueKey, rows };
    });
  const nextReview = useMemo(() => nextReviewLabel(weeklyReview), [weeklyReview]);
  const materialChange = useMemo(
    () => materialChangeSinceCritique(flags, plan, nowTs ?? 0),
    [flags, plan, nowTs],
  );

  const onPlan = greeting?.book.on_plan ?? null;

  return (
    <Card data-slot="plan-adherence">
      <CardHeader>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <CardTitle className="font-mono">Plan adherence</CardTitle>
          <div className="flex items-center gap-1.5">
            {onPlan !== null ? (
              <StatusPill
                tone={onPlan ? "success" : "warning"}
                mono
                data-testid="adherence-status"
              >
                {onPlan ? "ON PLAN" : "IN TRANSITION"}
              </StatusPill>
            ) : null}
            {critique?.overdue ? (
              <StatusPill tone="error" mono data-testid="adherence-overdue">
                refresh overdue
              </StatusPill>
            ) : null}
          </div>
        </div>
        {/* Human-readable plan name — never the internal draft slug. */}
        <CardDescription data-testid="adherence-plan-name">
          {formatPlanLabel(plan)
            ? `Current plan: ${formatPlanLabel(plan)}`
            : "No plan imported yet."}
        </CardDescription>
      </CardHeader>
      <CardContent className="text-sm text-muted-foreground flex flex-col gap-1.5">
        {/* Live deterministic status — the greeting's canonical
            on-plan computation (allocation bands + concentration cap),
            assembled server-side. */}
        {greeting?.book.on_plan_note ? (
          <p className="font-mono text-xs" data-testid="adherence-note">
            {greeting.book.on_plan_note}
          </p>
        ) : null}

        {/* Critique verdict + age + next auto-review. */}
        <div className="flex items-center gap-2 flex-wrap">
          <p
            className="font-mono text-xs tabular-nums"
            data-testid="adherence-critique"
          >
            {critique
              ? `critique: ${critique.total} finding${critique.total === 1 ? "" : "s"}` +
                (critique.red > 0 ? ` · ${critique.red} RED` : "") +
                (critique.yellow > 0 ? ` · ${critique.yellow} YELLOW` : "") +
                (critique.createdLabel
                  ? ` · ${critique.createdLabel}` +
                    (critique.ageDays !== null
                      ? ` (${critique.ageDays}d ago)`
                      : "")
                  : critique.ageDays !== null
                    ? ` · ${critique.ageDays}d ago`
                    : "")
              : "no critique on file yet"}
            {nextReview ? ` · next auto-review ${nextReview}` : ""}
          </p>
          {findings.length > 0 ? (
            <button
              type="button"
              className="font-mono text-xs underline underline-offset-2 text-muted-foreground hover:text-foreground"
              data-testid="adherence-findings-toggle"
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? "hide findings" : "show findings"}
            </button>
          ) : null}
        </div>

        {/* Reconcile-loop outcome — "what Argosy did about the findings". */}
        {reconcile?.summary_line ? (
          <p
            className="font-mono text-xs tabular-nums"
            data-testid="adherence-reconcile"
          >
            {reconcile.summary_line}
          </p>
        ) : null}

        {/* Expandable findings list — REDs first, one scannable row per
            finding (severity chip · bold topic · one-line summary ·
            optional reconcile outcome tag), full text behind a per-row
            expand. Data is already on the /api/plan/current DTO. */}
        {expanded && findings.length > 0 ? (
          <ul
            className="flex flex-col gap-1.5 mt-1"
            data-testid="adherence-findings"
          >
            {findings.map((f, i) => {
              const open = openRows.has(i);
              const hasDetail = Boolean(
                f.plan_item_ref ||
                  (f.evidence && f.evidence.length > 0) ||
                  f.recommended_action,
              );
              return (
                <li key={i} className="flex flex-col gap-1">
                  <div className="flex items-start gap-2">
                    <StatusPill tone={severityTone(f.severity)} mono>
                      {f.severity ?? "?"}
                    </StatusPill>
                    <span className="text-xs min-w-0">
                      {f.topic ? (
                        <span className="font-medium text-foreground">
                          {f.topic}
                          {" — "}
                        </span>
                      ) : null}
                      {f.summary ?? f.plan_item_ref ?? ""}
                    </span>
                    {f.outcome ? (
                      <StatusPill
                        tone={f.outcome === "unresolved" ? "error" : "neutral"}
                        mono
                        data-testid={`adherence-finding-outcome-${i}`}
                      >
                        {f.outcome}
                      </StatusPill>
                    ) : null}
                    {hasDetail ? (
                      <button
                        type="button"
                        className="font-mono text-xs underline underline-offset-2 text-muted-foreground hover:text-foreground shrink-0"
                        data-testid={`adherence-finding-toggle-${i}`}
                        onClick={() => toggleRow(i)}
                      >
                        {open ? "less" : "more"}
                      </button>
                    ) : null}
                  </div>
                  {open ? (
                    <div
                      className="ml-2 pl-3 border-l text-xs flex flex-col gap-1"
                      data-testid={`adherence-finding-detail-${i}`}
                    >
                      {f.plan_item_ref ? (
                        <p className="font-mono text-muted-foreground">
                          {f.plan_item_ref}
                        </p>
                      ) : null}
                      {(f.evidence ?? []).map((e, j) => (
                        <p key={j}>{e}</p>
                      ))}
                      {f.recommended_action ? (
                        <p className="text-foreground">
                          Recommended: {f.recommended_action}
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : null}

        {/* Run + deep-link row. */}
        <div className="flex items-center gap-3 mt-1 flex-wrap">
          <Button
            size="sm"
            variant="secondary"
            disabled={reviewRunning}
            data-testid="adherence-run-review"
            onClick={runReviewNow}
          >
            {reviewRunning ? "Reviewing… (minutes)" : "Run review now"}
          </Button>
          {critique ? (
            <a
              href="/plan#critique"
              className="font-mono text-xs underline underline-offset-2 text-muted-foreground hover:text-foreground"
              data-testid="adherence-full-report"
            >
              full report →
            </a>
          ) : null}
        </div>
        {reviewError ? (
          <p
            className="font-mono text-xs text-destructive"
            data-testid="adherence-review-error"
          >
            review failed: {reviewError}
          </p>
        ) : null}

        {/* Material change since the last critique — surfaced from the
            state observer's allocation/concentration flags. */}
        {materialChange ? (
          <p
            className="font-mono text-xs text-warning"
            data-testid="adherence-material-change"
          >
            Material allocation change observed since the last critique —
            flagged for the next auto-review.
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
