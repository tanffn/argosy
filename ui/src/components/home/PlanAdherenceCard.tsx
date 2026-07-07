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

interface CritiqueShape {
  overall_summary?: string;
  findings?: { severity?: string }[];
}

export interface CritiqueLine {
  total: number;
  red: number;
  yellow: number;
  ageDays: number | null;
  overdue: boolean;
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
  if (plan?.latest_critique_created_at) {
    const t = new Date(plan.latest_critique_created_at).getTime();
    if (!Number.isNaN(t)) {
      ageDays = Math.max(0, Math.floor((now - t) / (24 * 3600 * 1000)));
      overdue = now - t > CRITIQUE_OVERDUE_MS;
    }
  }
  return { total: findings.length, red, yellow, ageDays, overdue };
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
  const cron = job.metadata.schedule_cron;
  if (cron) {
    const m = cron.trim().match(/^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+(\w{3})$/i);
    if (m) {
      const [, min, hour, dow] = m;
      const day = dow.charAt(0).toUpperCase() + dow.slice(1).toLowerCase();
      return `${day} ${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;
    }
  }
  return job.metadata.schedule_human || null;
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

export function PlanAdherenceCard({ userId, plan, greeting }: Props) {
  const [weeklyReview, setWeeklyReview] = useState<JobView | null>(null);
  const [flags, setFlags] = useState<MonitorFlagDTO[]>([]);
  // Clock stamped from the effect so render stays pure.
  const [nowTs, setNowTs] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([api.jobs.list(), api.monitorFlags(userId)]).then(
      ([jobsRes, flagsRes]) => {
        if (cancelled) return;
        if (jobsRes.status === "fulfilled") {
          setWeeklyReview(
            jobsRes.value.jobs.find(
              (j) => j.metadata.name === "weekly_review",
            ) ?? null,
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
        <p className="font-mono text-xs tabular-nums" data-testid="adherence-critique">
          {critique
            ? `critique: ${critique.total} finding${critique.total === 1 ? "" : "s"}` +
              (critique.red > 0 ? ` · ${critique.red} RED` : "") +
              (critique.yellow > 0 ? ` · ${critique.yellow} YELLOW` : "") +
              (critique.ageDays !== null ? ` · ${critique.ageDays}d ago` : "")
            : "no critique on file yet"}
          {nextReview ? ` · next auto-review ${nextReview}` : ""}
        </p>

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
