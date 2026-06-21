"use client";

/**
 * FiCrossingHero — the flagship "are you FI yet?" visual.
 *
 * A progress meter (progress_pct toward 100% of the FI target) stacked
 * over a compact Recharts LineChart of the deterministic forward liquid-
 * wealth projection. A horizontal ReferenceLine marks the FI target and a
 * ReferenceDot marks the crossing year (where the projection meets target).
 *
 * Geometry is scale-invariant — bar/line proportions come straight from
 * `viz.data`. Any TEXTUAL number the user reads comes from the chapter's
 * facts[].display (formatted centrally by the backend fact registry); this
 * component only formats axis ticks, which it normalizes itself.
 */

import { useMemo } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  type TooltipContentProps,
  XAxis,
  YAxis,
} from "recharts";

interface FiCrossingPoint {
  year: number;
  projected_liquid_nis: number;
}

export interface FiCrossingData {
  progress_pct: number | null;
  target_nis: number | null;
  series: FiCrossingPoint[];
  crossing_year: number | null;
}

const LINE_COLOR = "#6366f1"; // indigo — projected liquid wealth
const TARGET_COLOR = "#f97316"; // orange — FI target
const CROSSING_COLOR = "#10b981"; // emerald — crossing dot

function fmtNis(v: unknown): string {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `₪${(n / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `₪${(n / 1_000).toFixed(0)}K`;
  return `₪${n.toFixed(0)}`;
}

export function FiCrossingHero({ data }: { data: FiCrossingData }) {
  const series = useMemo(
    () => (Array.isArray(data.series) ? data.series : []),
    [data.series],
  );

  // Crossing dot reads its y off the matching series point so it sits on
  // the line rather than floating.
  const crossingPoint = useMemo<FiCrossingPoint | null>(() => {
    if (data.crossing_year == null) return null;
    const hit = series.find((p) => p.year === data.crossing_year);
    if (hit) return hit;
    // No exact tick — anchor it on the target line at the crossing year.
    if (data.target_nis != null) {
      return { year: data.crossing_year, projected_liquid_nis: data.target_nis };
    }
    return null;
  }, [series, data.crossing_year, data.target_nis]);

  const pct = data.progress_pct;
  const clampedPct =
    typeof pct === "number" && Number.isFinite(pct)
      ? Math.max(0, Math.min(100, pct))
      : null;

  const renderTooltip = (tp: TooltipContentProps) => {
    if (!tp.active || !tp.payload || tp.payload.length === 0) return null;
    const row = tp.payload[0]?.payload as FiCrossingPoint | undefined;
    if (!row) return null;
    return (
      <div className="rounded-md border border-border/60 bg-background/95 px-3 py-2 text-xs shadow-sm">
        <div className="font-mono text-[10px] text-muted-foreground">
          {row.year}
        </div>
        <div className="mt-1 font-mono">{fmtNis(row.projected_liquid_nis)}</div>
      </div>
    );
  };

  return (
    <div className="flex flex-col gap-4">
      {/* Progress meter toward the FI target. */}
      <div>
        <div className="flex items-baseline justify-between text-xs">
          <span className="font-mono uppercase tracking-wider text-muted-foreground">
            Progress to financial independence
          </span>
          <span className="font-mono font-medium text-foreground">
            {clampedPct == null ? "—" : `${clampedPct.toFixed(0)}%`}
          </span>
        </div>
        <div className="mt-1.5 h-3 w-full overflow-hidden rounded-full bg-secondary/60">
          <div
            className="h-full rounded-full bg-success transition-[width] duration-500 ease-out"
            style={{ width: `${clampedPct ?? 0}%` }}
          />
        </div>
      </div>

      {/* Forward liquid-wealth projection with target + crossing. */}
      {series.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">
          Projection not available yet.
        </p>
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <LineChart
            data={series}
            margin={{ top: 12, right: 16, bottom: 4, left: 0 }}
          >
            <CartesianGrid strokeDasharray="3 3" opacity={0.18} />
            <XAxis
              dataKey="year"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(v: number) => `${Math.round(v)}`}
              fontSize={11}
              allowDecimals={false}
            />
            <YAxis
              fontSize={10}
              width={56}
              tickFormatter={(v) => fmtNis(v)}
            />
            <Tooltip content={renderTooltip} cursor={false} />
            {data.target_nis != null && (
              <ReferenceLine
                y={data.target_nis}
                stroke={TARGET_COLOR}
                strokeDasharray="4 4"
                label={{
                  value: `FI target ${fmtNis(data.target_nis)}`,
                  position: "insideTopRight",
                  fill: TARGET_COLOR,
                  fontSize: 10,
                }}
              />
            )}
            <Line
              type="monotone"
              dataKey="projected_liquid_nis"
              stroke={LINE_COLOR}
              strokeWidth={2.5}
              dot={false}
              isAnimationActive={false}
            />
            {crossingPoint != null && (
              <ReferenceDot
                x={crossingPoint.year}
                y={crossingPoint.projected_liquid_nis}
                r={6}
                fill={CROSSING_COLOR}
                stroke="#047857"
                strokeWidth={2}
                label={{
                  value: `crossing ${crossingPoint.year}`,
                  position: "top",
                  fill: CROSSING_COLOR,
                  fontSize: 10,
                }}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
