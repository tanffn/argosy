"use client";

/**
 * WealthTrajectoryCard — what an FM would actually show for "how is the
 * book doing": the past 12 months ACTUAL net worth (portfolio_snapshots
 * history via GET /api/portfolio/net-worth-history) plus ~2 years
 * PROJECTED from the wealth-dashboard's canonical trajectory (the same
 * scenario engine the retirement surfaces bind to), rendered as a
 * dashed median line inside a shaded bear→typical band, clearly
 * labelled "projected".
 *
 * Units: USD. The wealth-dashboard trajectory is NIS, converted here
 * with the dashboard's own fx_usd_nis assumption so both halves of the
 * chart share one canonical FX source. When FX is unavailable the
 * projection is omitted rather than plotted in mixed units.
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

import {
  api,
  type NetWorthHistoryResponse,
  type WealthDashboardDTO,
} from "@/lib/api";

const PROJECTION_YEARS = 2;

const ACTUAL_COLOR = "#10b981"; // emerald — realized
const PROJECTED_COLOR = "#6366f1"; // indigo — model output

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

export function buildRows(
  history: NetWorthHistoryResponse | null,
  dash: WealthDashboardDTO | null,
  now: number = Date.now(),
): Row[] {
  const rows: Row[] = [];
  for (const p of history?.points ?? []) {
    if (p.total_usd === null) continue;
    const ts = new Date(p.date).getTime();
    if (Number.isNaN(ts)) continue;
    rows.push({ ts, actual: p.total_usd });
  }
  const fx = dash?.assumptions.fx_usd_nis ?? null;
  const traj = dash?.retirement.trajectory ?? [];
  if (fx !== null && fx > 0 && traj.length > 0) {
    const yearMs = 365.25 * 24 * 3600 * 1000;
    for (const t of traj) {
      if (t.year > PROJECTION_YEARS) break;
      const low = Math.min(t.bear, t.typical) / fx;
      const high = Math.max(t.bear, t.typical) / fx;
      rows.push({
        ts: now + t.year * yearMs,
        projMid: t.typical / fx,
        projBand: [low, high],
      });
    }
  }
  return rows.sort((a, b) => a.ts - b.ts);
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

  const rows = useMemo(
    () => buildRows(history, dash, nowTs ?? 0),
    [history, dash, nowTs],
  );
  const hasActual = rows.some((r) => r.actual !== undefined);
  const hasProjection = rows.some((r) => r.projMid !== undefined);
  const latestActual = [...rows].reverse().find((r) => r.actual !== undefined);

  return (
    <div
      className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-1"
      data-slot="wealth-trajectory"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
          Wealth trajectory
        </span>
        {latestActual?.actual !== undefined ? (
          <span
            className="font-mono text-sm font-semibold tabular-nums"
            data-testid="wealth-latest"
          >
            {formatUsdCompact(latestActual.actual)}
          </span>
        ) : null}
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
                dot={{ r: 2 }}
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
            {hasActual ? "solid: last 12 months actual" : null}
            {hasActual && hasProjection ? " · " : null}
            {hasProjection
              ? `dashed/shaded: ~${PROJECTION_YEARS}y projected (canonical scenario engine)`
              : null}
          </div>
        </>
      )}
    </div>
  );
}
