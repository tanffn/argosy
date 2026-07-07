"use client";

/**
 * WealthTrajectoryCard — what an FM would actually show for "how is the
 * book doing", at QUARTERLY resolution: exactly 1 year of ACTUAL net
 * worth (portfolio_snapshots history via GET
 * /api/portfolio/net-worth-history, bucketed to the last point per
 * quarter) plus exactly 1 year PROJECTED from the wealth-dashboard's
 * canonical scenario trajectory (yearly points, interpolated
 * geometrically to quarters), rendered as a dashed median line inside a
 * shaded bear→typical band clearly labelled "projected".
 *
 * The header pill answers "are we beating the trendline": a linear fit
 * through the past year's actuals, evaluated at the latest snapshot —
 * AHEAD OF TREND +$X / BEHIND TREND −$X.
 *
 * Universe: the PORTFOLIO BOOK (labelled so). The projection applies
 * the trajectory's growth RATES anchored at the latest actual — its
 * absolute levels are total net worth (pensions + real estate) and
 * would jump ~$0.7M at today if plotted directly. See buildProjection.
 */

import { useEffect, useMemo, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type NetWorthHistoryResponse,
  type WealthDashboardDTO,
} from "@/lib/api";

const PAST_QUARTERS = 4; // exactly 1 year back
const PROJECTION_QUARTERS = 4; // exactly 1 year forward

const ACTUAL_COLOR = "#10b981"; // emerald — realized
const PROJECTED_COLOR = "#6366f1"; // indigo — model output

const QUARTER_MS = (365.25 / 4) * 24 * 3600 * 1000;

interface Row {
  ts: number; // epoch ms — numeric x-axis handles irregular spacing
  actual?: number;
  projMid?: number;
  projBand?: [number, number];
}

export function formatUsdCompact(v: number): string {
  if (!Number.isFinite(v)) return "—";
  if (Math.abs(v) >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  return `$${Math.round(v / 1_000).toLocaleString()}K`;
}

function monthLabel(ts: number): string {
  const d = new Date(ts);
  return d.toLocaleDateString([], { month: "short", year: "2-digit" });
}

/**
 * Quarterly actuals: the LAST snapshot per calendar quarter, capped to
 * the past PAST_QUARTERS year-window ending "now". The latest snapshot
 * always survives (it is the last point of its own quarter).
 */
export function bucketQuarterly(
  history: NetWorthHistoryResponse | null,
  now: number,
): Row[] {
  const cutoff = now - PAST_QUARTERS * QUARTER_MS;
  const byQuarter = new Map<string, Row>();
  for (const p of history?.points ?? []) {
    if (p.total_usd === null) continue;
    const ts = new Date(p.date).getTime();
    if (Number.isNaN(ts) || ts < cutoff || ts > now) continue;
    const d = new Date(ts);
    const key = `${d.getFullYear()}-q${Math.floor(d.getMonth() / 3)}`;
    const prev = byQuarter.get(key);
    if (!prev || ts > prev.ts) byQuarter.set(key, { ts, actual: p.total_usd });
  }
  return [...byQuarter.values()].sort((a, b) => a.ts - b.ts);
}

/**
 * Quarterly projection ANCHORED at the latest actual: the canonical
 * trajectory's year-0 → year-1 GROWTH RATES (per band, geometric
 * quarterly interpolation) applied forward from ``anchor`` — the latest
 * snapshot's book value. This keeps the whole chart in ONE universe
 * (the portfolio book): the trajectory's absolute levels are TOTAL net
 * worth (pensions + real estate included — live ~$4.7M vs the ~$4.0M
 * book), so plotting them directly made the projection "jump" at
 * today. Growth rates are unitless, so anchored mode needs no FX.
 *
 * Fallback (no actuals to anchor on): absolute levels converted
 * NIS→USD by the dashboard's own fx — still a single-universe chart,
 * since without actuals there is nothing to disagree with. Omitted
 * when fx is missing (never mixed units).
 */
export function buildProjection(
  dash: WealthDashboardDTO | null,
  now: number,
  anchor: number | null,
): Row[] {
  const traj = dash?.retirement.trajectory ?? [];
  const y0 = traj.find((t) => t.year === 0);
  const y1 = traj.find((t) => t.year === 1);
  if (!y0 || !y1) return [];
  const rows: Row[] = [];

  if (anchor !== null && anchor > 0 && y0.typical > 0 && y0.bear > 0) {
    // Geometric quarterly growth ratio between year-0 and year-1 levels.
    const ratio = (a: number, b: number, q: number): number =>
      b > 0 ? Math.pow(b / a, q / 4) : 1;
    for (let q = 0; q <= PROJECTION_QUARTERS; q++) {
      const typical = anchor * ratio(y0.typical, y1.typical, q);
      const bear = anchor * ratio(y0.bear, y1.bear, q);
      rows.push({
        ts: now + q * QUARTER_MS,
        projMid: typical,
        projBand: [Math.min(bear, typical), Math.max(bear, typical)],
      });
    }
    return rows;
  }

  const fx = dash?.assumptions.fx_usd_nis ?? null;
  if (fx === null || fx <= 0) return [];
  const interp = (a: number, b: number, q: number): number =>
    a > 0 && b > 0 ? a * Math.pow(b / a, q / 4) : a + (b - a) * (q / 4);
  for (let q = 0; q <= PROJECTION_QUARTERS; q++) {
    const bear = interp(y0.bear, y1.bear, q) / fx;
    const typical = interp(y0.typical, y1.typical, q) / fx;
    rows.push({
      ts: now + q * QUARTER_MS,
      projMid: typical,
      projBand: [Math.min(bear, typical), Math.max(bear, typical)],
    });
  }
  return rows;
}

/**
 * "Are we beating the trendline" — least-squares linear fit through the
 * past year's quarterly actuals, evaluated at the latest actual point.
 * Returns the residual (latest − fit); null when fewer than 3 points
 * (a 2-point fit is exact, so the residual is vacuously 0).
 */
export function trendDelta(actuals: Row[]): number | null {
  const pts = actuals.filter((r) => r.actual !== undefined);
  if (pts.length < 3) return null;
  const n = pts.length;
  // Quarter-scaled, origin-shifted x for numeric stability.
  const x0 = pts[0].ts;
  let sx = 0;
  let sy = 0;
  let sxx = 0;
  let sxy = 0;
  for (const p of pts) {
    const x = (p.ts - x0) / QUARTER_MS;
    const y = p.actual!;
    sx += x;
    sy += y;
    sxx += x * x;
    sxy += x * y;
  }
  const denom = n * sxx - sx * sx;
  if (denom === 0) return null;
  const b = (n * sxy - sx * sy) / denom;
  const a = (sy - b * sx) / n;
  const last = pts[n - 1];
  const fit = a + b * ((last.ts - x0) / QUARTER_MS);
  return last.actual! - fit;
}

function TrajectoryTooltip(props: {
  active?: boolean;
  label?: number;
  payload?: { name?: string; value?: number | number[]; color?: string }[];
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
              {Array.isArray(entry.value)
                ? `${formatUsdCompact(entry.value[0])} – ${formatUsdCompact(entry.value[1])}`
                : typeof entry.value === "number"
                  ? formatUsdCompact(entry.value)
                  : "—"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function WealthTrajectoryCard({ userId }: { userId: string }) {
  const [history, setHistory] = useState<NetWorthHistoryResponse | null>(null);
  const [dash, setDash] = useState<WealthDashboardDTO | null>(null);
  const [loading, setLoading] = useState(true);
  // Projection anchor "now" — stamped from the fetch effect so render
  // stays pure (react-hooks/purity forbids Date.now() during render).
  const [nowTs, setNowTs] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([
      api.netWorthHistory(userId, 12),
      api.wealthDashboard(userId),
    ]).then(([h, d]) => {
      if (cancelled) return;
      if (h.status === "fulfilled") setHistory(h.value);
      if (d.status === "fulfilled") setDash(d.value);
      setNowTs(Date.now());
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const actuals = useMemo(
    () => bucketQuarterly(history, nowTs ?? 0),
    [history, nowTs],
  );
  // Anchor the projection at the latest ACTUAL book value — the whole
  // chart lives in one universe (the portfolio book), and the projected
  // line starts where the actual line ends instead of jumping to the
  // trajectory's total-net-worth level.
  const anchor = useMemo(() => {
    const last = actuals.length > 0 ? actuals[actuals.length - 1] : null;
    return last?.actual ?? null;
  }, [actuals]);
  const projection = useMemo(
    () => buildProjection(dash, nowTs ?? 0, anchor),
    [dash, nowTs, anchor],
  );
  const rows = useMemo(
    () => [...actuals, ...projection].sort((a, b) => a.ts - b.ts),
    [actuals, projection],
  );
  const delta = useMemo(() => trendDelta(actuals), [actuals]);

  const hasActual = actuals.length > 0;
  const hasProjection = projection.length > 0;
  const latestActual = hasActual ? actuals[actuals.length - 1] : null;

  // Ingestion only started recently — say so instead of implying a
  // silently missing year of history (~2 months of slack on the window).
  const historyBeginsNote = useMemo(() => {
    if (actuals.length === 0 || nowTs === null) return null;
    const first = actuals[0];
    if (first.ts > nowTs - (PAST_QUARTERS - 0.7) * QUARTER_MS) {
      const label = new Date(first.ts).toLocaleDateString([], {
        month: "short",
        year: "numeric",
      });
      return `history begins ${label}`;
    }
    return null;
  }, [actuals, nowTs]);

  return (
    <div
      className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-1"
      data-slot="wealth-trajectory"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
          Wealth trajectory — portfolio (book)
        </span>
        <div className="flex items-center gap-2">
          {latestActual?.actual !== undefined ? (
            <span
              className="font-mono text-sm font-semibold tabular-nums"
              data-testid="wealth-latest"
            >
              {formatUsdCompact(latestActual.actual)}
            </span>
          ) : null}
          {delta !== null ? (
            Math.abs(delta) < 500 ? (
              <StatusPill tone="success" mono data-testid="wealth-trend">
                ON TREND
              </StatusPill>
            ) : (
              <StatusPill
                tone={delta >= 0 ? "success" : "warning"}
                mono
                data-testid="wealth-trend"
                title="latest snapshot vs a linear fit through the past year's quarterly actuals"
              >
                {delta >= 0
                  ? `AHEAD OF TREND +${formatUsdCompact(delta)}`
                  : `BEHIND TREND −${formatUsdCompact(Math.abs(delta))}`}
              </StatusPill>
            )
          ) : null}
        </div>
      </div>
      {loading ? (
        <div
          className="h-[180px] flex items-center justify-center text-xs text-muted-foreground font-mono"
          data-testid="wealth-loading"
        >
          loading…
        </div>
      ) : rows.length === 0 ? (
        <div
          className="h-[180px] flex items-center justify-center text-xs text-muted-foreground font-mono"
          data-testid="wealth-empty"
        >
          No snapshot history yet.
        </div>
      ) : (
        <>
          <ResponsiveContainer width="100%" height={180}>
            <ComposedChart
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
                width={52}
                tickFormatter={formatUsdCompact}
                domain={["auto", "auto"]}
              />
              <Tooltip content={<TrajectoryTooltip />} />
              {hasProjection ? (
                <Area
                  dataKey="projBand"
                  name="projected (bear–typical)"
                  stroke="none"
                  fill={PROJECTED_COLOR}
                  fillOpacity={0.15}
                  connectNulls
                  isAnimationActive={false}
                />
              ) : null}
              <Line
                dataKey="actual"
                name="actual"
                stroke={ACTUAL_COLOR}
                strokeWidth={2}
                dot={{ r: 3 }}
                connectNulls
                isAnimationActive={false}
              />
              {hasProjection ? (
                <Line
                  dataKey="projMid"
                  name="projected"
                  stroke={PROJECTED_COLOR}
                  strokeWidth={2}
                  strokeDasharray="5 4"
                  dot={false}
                  connectNulls
                  isAnimationActive={false}
                />
              ) : null}
            </ComposedChart>
          </ResponsiveContainer>
          <div className="text-[11px] text-muted-foreground tabular-nums">
            {hasActual ? "solid: past year actual (quarterly)" : null}
            {hasActual && hasProjection ? " · " : null}
            {hasProjection
              ? "dashed/shaded: 1y projected (canonical growth rates, anchored at the latest actual)"
              : null}
            {historyBeginsNote ? ` · ${historyBeginsNote}` : null}
          </div>
        </>
      )}
    </div>
  );
}
