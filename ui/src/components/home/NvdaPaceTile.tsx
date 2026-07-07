"use client";

/**
 * NvdaPaceTile — the home page's NVDA sell-down pace tile.
 *
 * The sell-down is managed per CALENDAR TAX YEAR (Israeli CGT is
 * assessed Jan–Dec), so on ``basis === "glide"`` with the tax-year
 * quota fields present the tile headlines the year quota:
 *
 *   "2026 target: sell ~4,810 sh by Dec 31 · 1,600 sold"
 *
 * with the next dated glide waypoint as the secondary checkpoint line
 * ("Next waypoint Oct 6: ≤47% — sell ~2,470 sh by then"). The
 * calendar-YTD sold count is FIRST-CLASS (pre-plan sales count toward
 * the tax-year quota); a plan revision never resets the year. The old
 * "0 / 27 by day 2" daily pro-rata framing is exactly what this
 * replaces.
 *
 * - glide payloads WITHOUT the quota fields (legacy) fall back to the
 *   prior plan-relative copy ("X / Y shares · day N of the plan year").
 * - anything else ("horizon" fallback / legacy payloads) — the numbers
 *   ARE calendar-YTD pro-rated, so the calendar labels stay.
 *
 * Status badge: prefer the backend's one-word ``status`` (banded
 * generously against the year quota — waypoints are quarterly
 * commitments, not daily quotas); fall back to the old heuristic for
 * legacy payloads.
 */

import { useEffect, useState } from "react";

import { SectionHeader } from "@/components/ui/section-header";
import { StatusPill } from "@/components/ui/status-pill";
import type { NvdaPaceDTO } from "@/lib/api";

function pctOfYearElapsed(now: number): number {
  const d = new Date(now);
  const start = new Date(d.getFullYear(), 0, 1).getTime();
  const end = new Date(d.getFullYear() + 1, 0, 1).getTime();
  return ((now - start) / (end - start)) * 100;
}

/** 1-based day index inside the plan year (clamped at 1). */
function dayOfPlanYear(planStartIso: string, now: number): number | null {
  const start = new Date(`${planStartIso}T00:00:00`).getTime();
  if (Number.isNaN(start)) return null;
  return Math.max(1, Math.floor((now - start) / 86_400_000) + 1);
}

function shortDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function PaceBar({ pct, tone }: { pct: number; tone: "success" | "warning" }) {
  const fillClass = tone === "success" ? "bg-success" : "bg-warning";
  return (
    <div className="h-1 w-full rounded-full bg-secondary/60 overflow-hidden">
      <div
        className={`h-full ${fillClass} transition-all duration-500`}
        style={{ width: `${Math.max(0, Math.min(100, pct))}%` }}
      />
    </div>
  );
}

export function nvdaOnPace(pace: NvdaPaceDTO): boolean {
  // The backend's status is banded against the tax-year quota — trust it.
  if (pace.status === "behind") return false;
  if (pace.status === "on" || pace.status === "ahead") return true;
  // Legacy payloads: the old pct-of-target heuristic.
  const sold = pace.shares_sold_ytd ?? 0;
  const target = pace.target_shares_ytd ?? 0;
  if (target <= 0) return false;
  if (pace.on_track || sold >= target) return true;
  const underPct = ((target - sold) / target) * 100;
  return underPct < 20;
}

export function NvdaPaceTile({
  pace,
  now,
}: {
  pace: NvdaPaceDTO;
  /** Epoch ms "today" override for tests; defaults to Date.now(). */
  now?: number;
}) {
  // "Today" is stamped from an effect so render stays pure
  // (react-hooks/purity forbids Date.now() during render), deferred to
  // the next frame so the effect body never calls setState synchronously
  // (react-hooks/set-state-in-effect) — same pattern as LiveClock /
  // WealthTrajectoryCard.
  const [stampedNow, setStampedNow] = useState<number | null>(null);
  useEffect(() => {
    let cancelled = false;
    const raf = window.requestAnimationFrame(() => {
      if (!cancelled) setStampedNow(Date.now());
    });
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(raf);
    };
  }, []);
  const ts = now ?? stampedNow;
  const sold = pace.shares_sold_ytd ?? 0;
  const target = pace.target_shares_ytd ?? 0;
  if (target <= 0 || ts === null) return null;

  const onPace = nvdaOnPace(pace);
  const tone = onPace ? "success" : "warning";
  const pillText =
    pace.status === "ahead"
      ? "AHEAD OF PACE"
      : onPace
        ? "ON PACE"
        : "BEHIND PACE";

  // --- Tax-year quota framing (glide basis with quota fields) --------
  const quota = pace.year_target_shares ?? 0;
  const taxYear = pace.tax_year ?? null;
  const quotaMode = pace.basis === "glide" && quota > 0 && taxYear !== null;

  if (quotaMode) {
    const soldCalendar = pace.sold_calendar_ytd ?? sold;
    const pctOfQuota = (soldCalendar / quota) * 100;
    const wpDate = pace.next_waypoint_date ?? null;
    const wpWeight = pace.next_waypoint_weight_pct ?? null;
    const wpShares = pace.shares_to_sell_by_waypoint ?? null;
    return (
      <section data-testid="nvda-pace-tile">
        <SectionHeader
          label="NVDA PACE"
          action={
            <StatusPill tone={tone} mono>
              {pillText}
            </StatusPill>
          }
        />
        <div className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-2">
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <div className="font-mono text-sm tabular-nums">
              {`${taxYear} target: sell ~${quota.toLocaleString()} sh by Dec 31 · ${soldCalendar.toLocaleString()} sold`}
            </div>
            <div className="text-[11px] text-muted-foreground tabular-nums">
              {`${pctOfQuota.toFixed(0)}% of quota · ${pctOfYearElapsed(ts).toFixed(0)}% of year elapsed`}
            </div>
          </div>
          <PaceBar pct={pctOfQuota} tone={tone} />
          {wpDate !== null && wpWeight !== null && wpShares !== null ? (
            <div className="text-xs text-muted-foreground tabular-nums">
              {`Next waypoint ${shortDate(wpDate)}: ≤${wpWeight.toFixed(0)}% — sell ~${wpShares.toLocaleString()} sh by then`}
            </div>
          ) : null}
        </div>
      </section>
    );
  }

  // --- Legacy renderings ---------------------------------------------
  const pctSold = (sold / target) * 100;
  const planDay =
    pace.basis === "glide" && pace.plan_start
      ? dayOfPlanYear(pace.plan_start, ts)
      : null;
  const glide = planDay !== null && pace.plan_start != null;

  // Calendar context on the legacy glide basis: sales BEFORE the plan
  // window (the calendar figure minus the plan-window sold count).
  const prePlanSold =
    glide && typeof pace.sold_calendar_ytd === "number"
      ? Math.max(0, pace.sold_calendar_ytd - sold)
      : null;
  const planYear = glide
    ? new Date(`${pace.plan_start}T00:00:00`).getFullYear()
    : null;

  return (
    <section data-testid="nvda-pace-tile">
      <SectionHeader
        label="NVDA PACE"
        action={
          <StatusPill tone={tone} mono>
            {pillText}
          </StatusPill>
        }
      />
      <div className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-2">
        <div className="flex items-center justify-between gap-4 flex-wrap">
          <div className="font-mono text-sm tabular-nums">
            {glide
              ? `${sold.toLocaleString()} / ${target.toLocaleString()} shares · day ${planDay} of the plan year`
              : `${sold.toLocaleString()} / ${target.toLocaleString()} shares sold YTD (plan target)`}
          </div>
          <div className="text-[11px] text-muted-foreground tabular-nums">
            {glide
              ? `plan started ${shortDate(pace.plan_start!)} · ${onPace ? "on pace" : "behind pace"}`
              : `${pctSold.toFixed(1)}% of plan target · ${pctOfYearElapsed(ts).toFixed(0)}% of year elapsed`}
          </div>
        </div>
        <PaceBar pct={pctSold} tone={tone} />
        {glide && prePlanSold !== null && prePlanSold > 0 ? (
          <div className="text-[11px] text-muted-foreground tabular-nums">
            {`${prePlanSold.toLocaleString()} sold in ${planYear} pre-plan`}
          </div>
        ) : null}
      </div>
    </section>
  );
}
