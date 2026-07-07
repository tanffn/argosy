"use client";

/**
 * DeconcentrationCard — "is the NVDA sell-down on schedule?" in one
 * glance. Plots the concentrated position's % of book over time:
 *
 *  - ACTUAL: direct NVDA % per historical snapshot
 *    (GET /api/portfolio/net-worth-history — snapshot history is
 *    positions-only, so this is direct-position weight, not fund
 *    look-through).
 *  - PLAN GLIDE: the TargetAllocationDoc glide waypoints, from the same
 *    source /plan renders (GET /api/plan/current/allocation-glidepath),
 *    taking the NVDA band's stitched trajectory.
 *
 * A status pill compares the latest actual against the glide value due
 * today: at/below (with a small tolerance) = ON SCHEDULE, above = BEHIND.
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

/** Tolerance band (percentage points) before "above glide" reads BEHIND. */
const ON_SCHEDULE_TOLERANCE_PP = 1.0;

interface Row {
  ts: number;
  actual?: number;
  glide?: number;
}

/** Pick the glidepath band that is the concentrated NVDA position. */
export function findNvdaClass(
  glidepath: AllocationGlidepathResponse | null,
): string | null {
  if (!glidepath) return null;
  const classes = glidepath.asset_classes;
  return (
    classes.find((c) => c.toLowerCase().includes("nvda")) ??
    classes.find((c) => c.toLowerCase().includes("individual stocks")) ??
    null
  );
}

export function buildRows(
  history: NetWorthHistoryResponse | null,
  glidepath: AllocationGlidepathResponse | null,
): Row[] {
  const rows: Row[] = [];
  for (const p of history?.points ?? []) {
    if (p.nvda_pct === null) continue;
    const ts = new Date(p.date).getTime();
    if (Number.isNaN(ts)) continue;
    rows.push({ ts, actual: p.nvda_pct });
  }
  const cls = findNvdaClass(glidepath);
  if (cls && glidepath) {
    for (const p of glidepath.points) {
      const v = p.composition_pct_by_class[cls];
      if (typeof v !== "number") continue;
      const ts = new Date(p.date).getTime();
      if (Number.isNaN(ts)) continue;
      rows.push({ ts, glide: v });
    }
  }
  return rows.sort((a, b) => a.ts - b.ts);
}

/**
 * ON SCHEDULE / BEHIND verdict: latest actual vs the glide value in
 * force at that moment (last glide point at-or-before the latest
 * actual, else the first glide point). Null when either side missing.
 */
export function scheduleVerdict(rows: Row[]): "on" | "behind" | null {
  const actuals = rows.filter((r) => r.actual !== undefined);
  const glides = rows.filter((r) => r.glide !== undefined);
  if (actuals.length === 0 || glides.length === 0) return null;
  const latest = actuals[actuals.length - 1];
  const due =
    [...glides].reverse().find((g) => g.ts <= latest.ts) ?? glides[0];
  return latest.actual! <= due.glide! + ON_SCHEDULE_TOLERANCE_PP
    ? "on"
    : "behind";
}

function monthLabel(ts: number): string {
  return new Date(ts).toLocaleDateString([], {
    month: "short",
    year: "2-digit",
  });
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

  const rows = useMemo(() => buildRows(history, glidepath), [history, glidepath]);
  const verdict = useMemo(() => scheduleVerdict(rows), [rows]);
  const hasGlide = rows.some((r) => r.glide !== undefined);
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
          Deconcentration — NVDA % of book
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
            >
              {verdict === "on" ? "ON SCHEDULE" : "BEHIND GLIDE"}
            </StatusPill>
          ) : null}
        </div>
      </div>
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
                name="actual (direct position)"
                stroke={ACTUAL_COLOR}
                strokeWidth={2}
                dot={{ r: 2 }}
                connectNulls
                isAnimationActive={false}
              />
              {hasGlide ? (
                <Line
                  dataKey="glide"
                  name="plan glide"
                  stroke={GLIDE_COLOR}
                  strokeWidth={2}
                  strokeDasharray="5 4"
                  dot={false}
                  connectNulls
                  isAnimationActive={false}
                />
              ) : null}
            </LineChart>
          </ResponsiveContainer>
          <div className="text-[11px] text-muted-foreground tabular-nums">
            solid: snapshot history
            {hasGlide ? " · dashed: plan glide waypoints" : " · plan glide unavailable"}
          </div>
        </>
      )}
    </div>
  );
}
