"use client";

/**
 * DeconcentrationCard — "is the NVDA sell-down on schedule?" in one
 * glance. Plots the concentrated position's % of the TRADEABLE book:
 *
 *  - ACTUAL: canonical NVDA concentration per historical snapshot
 *    (GET /api/portfolio/net-worth-history — nvda_concentration_pct,
 *    i.e. NVDA ÷ tradeable securities book, the SAME denominator the
 *    plan glide uses; direct-position weight, not fund look-through).
 *  - PLAN GLIDE: the TargetAllocationDoc's quarterly glide waypoints,
 *    from the same source /plan renders
 *    (GET /api/plan/current/allocation-glidepath). Each glide point IS
 *    a dated plan waypoint — rendered as a marker dot, and listed under
 *    the chart as "due <date>: ≤N%".
 *
 * The status pill compares the latest actual against the waypoint
 * CURRENTLY DUE (the last waypoint dated at-or-before the latest
 * snapshot — never the end-state target). Upticks between waypoints are
 * normal price drift between sales; the footer says so explicitly.
 */

import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type AllocationGlidepathResponse,
  type NetWorthHistoryResponse,
} from "@/lib/api";

const ACTUAL_COLOR = "#f97316"; // orange — today's over-weight (matches NvdaWinddown)
const GLIDE_COLOR = "#10b981"; // emerald — plan target

/** Tolerance band (percentage points) before "above the due waypoint" reads BEHIND. */
const ON_SCHEDULE_TOLERANCE_PP = 1.0;

interface Row {
  ts: number;
  actual?: number;
  glide?: number;
}

export interface Waypoint {
  ts: number;
  date: string; // ISO
  target_pct: number;
}

/**
 * Pick the glidepath band that IS the concentrated NVDA position.
 *
 * Careful: several sleeves merely REFERENCE NVDA in their names —
 * "Global quality growth (ex-NVDA-dense)", "Individual Stocks
 * (non-NVDA, to redeploy)". A naive substring match grabbed the
 * alphabetically-first of those, so the chart drew the ex-NVDA-dense
 * sleeve's 4.7%→11% RAMP as "the NVDA glide dips to 5% then rises".
 * Exclusion-qualified names (ex-/non-NVDA) never qualify; among real
 * mentions the strategic single-stock sleeve wins.
 */
export function findNvdaClass(
  glidepath: AllocationGlidepathResponse | null,
): string | null {
  if (!glidepath) return null;
  const isExcluded = (lc: string) =>
    lc.includes("ex-nvda") || lc.includes("non-nvda") || lc.includes("ex nvda");
  const mentions = glidepath.asset_classes.filter((c) => {
    const lc = c.toLowerCase();
    return lc.includes("nvda") && !isExcluded(lc);
  });
  return (
    mentions.find((c) => {
      const lc = c.toLowerCase();
      return lc.includes("single-stock") || lc.includes("strategic");
    }) ??
    mentions[0] ??
    glidepath.asset_classes.find((c) => {
      const lc = c.toLowerCase();
      return lc.includes("individual stocks") && !isExcluded(lc);
    }) ??
    null
  );
}

/**
 * The plan's dated NVDA waypoints, straight from the glidepath points
 * (the doc-backed glidepath serves the TargetAllocationDoc's quarterly
 * waypoints verbatim — each point is a real waypoint, not an
 * interpolation).
 */
export function extractWaypoints(
  glidepath: AllocationGlidepathResponse | null,
): Waypoint[] {
  const cls = findNvdaClass(glidepath);
  if (!cls || !glidepath) return [];
  const out: Waypoint[] = [];
  for (const p of glidepath.points) {
    const v = p.composition_pct_by_class[cls];
    if (typeof v !== "number") continue;
    const ts = new Date(p.date).getTime();
    if (Number.isNaN(ts)) continue;
    out.push({ ts, date: p.date.slice(0, 10), target_pct: v });
  }
  return out.sort((a, b) => a.ts - b.ts);
}

const YEAR_MS = 365.25 * 24 * 3600 * 1000;

/**
 * Window: exactly 1 year of past actuals + the plan glide to its end
 * (the doc glide spans the plan's transition horizon — currently one
 * year — so the future edge is the glide's own last waypoint).
 */
export function buildRows(
  history: NetWorthHistoryResponse | null,
  waypoints: Waypoint[],
  now: number = Date.now(),
): Row[] {
  const cutoff = now - YEAR_MS;
  const rows: Row[] = [];
  for (const p of history?.points ?? []) {
    if (p.nvda_pct === null) continue;
    const ts = new Date(p.date).getTime();
    if (Number.isNaN(ts) || ts < cutoff) continue;
    rows.push({ ts, actual: p.nvda_pct });
  }
  for (const w of waypoints) {
    rows.push({ ts: w.ts, glide: w.target_pct });
  }
  return rows.sort((a, b) => a.ts - b.ts);
}

/**
 * The waypoint CURRENTLY DUE for the latest actual: the last waypoint
 * dated at-or-before it (falling back to the first waypoint when the
 * glide starts in the future). Null when either side is missing.
 */
export function dueWaypoint(
  rows: Row[],
  waypoints: Waypoint[],
): Waypoint | null {
  const actuals = rows.filter((r) => r.actual !== undefined);
  if (actuals.length === 0 || waypoints.length === 0) return null;
  const latest = actuals[actuals.length - 1];
  return (
    [...waypoints].reverse().find((w) => w.ts <= latest.ts) ?? waypoints[0]
  );
}

/** ON SCHEDULE / BEHIND verdict vs the currently-due waypoint. */
export function scheduleVerdict(
  rows: Row[],
  waypoints: Waypoint[],
): "on" | "behind" | null {
  const due = dueWaypoint(rows, waypoints);
  if (due === null) return null;
  const actuals = rows.filter((r) => r.actual !== undefined);
  const latest = actuals[actuals.length - 1];
  return latest.actual! <= due.target_pct + ON_SCHEDULE_TOLERANCE_PP
    ? "on"
    : "behind";
}

function monthLabel(ts: number): string {
  return new Date(ts).toLocaleDateString([], {
    month: "short",
    year: "2-digit",
  });
}

function shortDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function DeconTooltip(props: {
  active?: boolean;
  label?: number;
  payload?: { name?: string; value?: number; color?: string }[];
}) {
  const { active, payload, label } = props;
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="rounded-md border border-border/60 bg-popover text-popover-foreground text-xs shadow p-2">
      <p className="font-semibold mb-1">
        {typeof label === "number" ? monthLabel(label) : ""}
      </p>
      <ul className="flex flex-col gap-0.5">
        {payload.map((entry, i) => (
          <li key={i} className="flex items-baseline gap-2">
            <span
              className="w-2 h-2 rounded-sm inline-block"
              style={{ backgroundColor: entry.color }}
            />
            <span className="flex-1">{entry.name}</span>
            <span className="font-mono">
              {typeof entry.value === "number"
                ? `${entry.value.toFixed(1)}%`
                : "—"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function DeconcentrationCard({ userId }: { userId: string }) {
  const [history, setHistory] = useState<NetWorthHistoryResponse | null>(null);
  const [glidepath, setGlidepath] =
    useState<AllocationGlidepathResponse | null>(null);
  const [loading, setLoading] = useState(true);
  // "today" marker — stamped from the fetch effect (render stays pure).
  const [todayTs, setTodayTs] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([
      api.netWorthHistory(userId, 12),
      api.planCurrentAllocationGlidepath(userId),
    ]).then(([h, g]) => {
      if (cancelled) return;
      if (h.status === "fulfilled") setHistory(h.value);
      if (g.status === "fulfilled") setGlidepath(g.value);
      setTodayTs(Date.now());
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const waypoints = useMemo(() => extractWaypoints(glidepath), [glidepath]);
  const rows = useMemo(
    // todayTs is stamped in the same effect that sets history, so rows
    // recompute with the real clock as soon as data exists (0 = no data).
    () => buildRows(history, waypoints, todayTs ?? 0),
    [history, waypoints, todayTs],
  );
  const due = useMemo(() => dueWaypoint(rows, waypoints), [rows, waypoints]);
  const verdict = useMemo(
    () => scheduleVerdict(rows, waypoints),
    [rows, waypoints],
  );
  const hasGlide = waypoints.length > 0;
  const latestActual = [...rows]
    .reverse()
    .find((r) => r.actual !== undefined)?.actual;

  return (
    <div
      className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-1"
      data-slot="deconcentration"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
          Deconcentration — NVDA % of tradeable book
        </span>
        <div className="flex items-center gap-2">
          {latestActual !== undefined ? (
            <span
              className="font-mono text-sm font-semibold tabular-nums"
              data-testid="decon-latest"
            >
              {latestActual.toFixed(1)}%
            </span>
          ) : null}
          {verdict ? (
            <StatusPill
              tone={verdict === "on" ? "success" : "warning"}
              mono
              data-testid="decon-verdict"
              title={
                due
                  ? `vs the waypoint currently due: ≤${due.target_pct.toFixed(1)}% by ${due.date}`
                  : undefined
              }
            >
              {verdict === "on" ? "ON SCHEDULE" : "BEHIND WAYPOINT"}
            </StatusPill>
          ) : null}
        </div>
      </div>
      {due ? (
        <p
          className="text-[11px] text-muted-foreground font-mono tabular-nums"
          data-testid="decon-due-line"
        >
          due waypoint: ≤{due.target_pct.toFixed(1)}% by {shortDate(due.date)}
          {latestActual !== undefined
            ? ` · actual ${latestActual.toFixed(1)}%`
            : ""}
        </p>
      ) : null}
      {loading ? (
        <div
          className="h-[180px] flex items-center justify-center text-xs text-muted-foreground font-mono"
          data-testid="decon-loading"
        >
          loading…
        </div>
      ) : rows.length === 0 ? (
        <div
          className="h-[180px] flex items-center justify-center text-xs text-muted-foreground font-mono"
          data-testid="decon-empty"
        >
          No concentration history yet.
        </div>
      ) : (
        <>
          <ResponsiveContainer width="100%" height={180}>
            <LineChart
              data={rows}
              margin={{ top: 8, right: 8, bottom: 0, left: 4 }}
            >
              <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
              <XAxis
                dataKey="ts"
                type="number"
                scale="time"
                domain={["dataMin", "dataMax"]}
                tickFormatter={monthLabel}
                fontSize={10}
                minTickGap={30}
              />
              <YAxis
                fontSize={10}
                width={36}
                tickFormatter={(v: number) => `${Math.round(v)}%`}
                domain={[0, "auto"]}
              />
              <Tooltip content={<DeconTooltip />} />
              {todayTs !== null ? (
                <ReferenceLine
                  x={todayTs}
                  stroke="var(--color-muted-foreground, #888)"
                  strokeDasharray="2 4"
                  label={{
                    value: "today",
                    position: "insideTopRight",
                    fontSize: 10,
                  }}
                />
              ) : null}
              <Line
                dataKey="actual"
                name="actual (tradeable-book weight)"
                stroke={ACTUAL_COLOR}
                strokeWidth={2}
                dot={{ r: 2 }}
                connectNulls
                isAnimationActive={false}
              />
              {hasGlide ? (
                // Waypoint markers: every dot on this line IS a dated
                // plan waypoint (the doc glide is quarterly, not an
                // interpolated series).
                <Line
                  dataKey="glide"
                  name="plan waypoint"
                  stroke={GLIDE_COLOR}
                  strokeWidth={2}
                  strokeDasharray="5 4"
                  dot={{ r: 4, fill: GLIDE_COLOR, strokeWidth: 0 }}
                  connectNulls
                  isAnimationActive={false}
                />
              ) : null}
            </LineChart>
          </ResponsiveContainer>
          {hasGlide ? (
            <div
              className="flex flex-wrap gap-1.5"
              data-testid="decon-waypoints"
            >
              {waypoints.map((w) => (
                <span
                  key={w.date}
                  className="rounded-full border border-border bg-secondary/40 px-2 py-0.5 font-mono text-[10px] tabular-nums"
                  title={`plan waypoint: NVDA ≤${w.target_pct.toFixed(1)}% by ${w.date}`}
                >
                  {shortDate(w.date)} · ≤{Math.round(w.target_pct)}%
                </span>
              ))}
            </div>
          ) : null}
          <div className="text-[11px] text-muted-foreground tabular-nums">
            {hasGlide
              ? "upticks between waypoints are price drift between sales — the schedule is judged at the dated waypoints"
              : "solid: snapshot history · plan glide unavailable"}
          </div>
        </>
      )}
    </div>
  );
}
