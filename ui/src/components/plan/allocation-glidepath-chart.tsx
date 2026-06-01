"use client";

/**
 * Wave 8 Piece B2 — Allocation glidepath chart.
 *
 * Stacked-area chart of the synthesizer's per-asset-class target
 * trajectory over time, anchored at today's portfolio composition.
 * The backend service in argosy/services/allocation_glidepath.py
 * does the heavy lifting (waypoint stitching, direction-reversal
 * collapse, pct-scale normalisation); this component only renders
 * its output + surfaces the collapsed/excluded callouts so the user
 * understands why some targets aren't on the chart.
 */

import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  type TooltipContentProps,
  XAxis,
  YAxis,
} from "recharts";
type GlidepathTooltipProps = TooltipContentProps;

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type {
  AllocationGlidepathResponse,
  GlidepathPointDTO,
} from "@/lib/api";

interface AllocationGlidepathChartProps {
  response: AllocationGlidepathResponse | null;
}

// Palette mirrors the existing AllocationChart so the recap reads
// consistent with the other charts on /plan.
const BAND_COLORS = [
  "var(--color-chart-1, #6366f1)",
  "var(--color-chart-2, #22d3ee)",
  "var(--color-chart-3, #f59e0b)",
  "var(--color-chart-4, #10b981)",
  "var(--color-chart-5, #f43f5e)",
  "var(--color-chart-6, #8b5cf6)",
  "var(--color-chart-7, #14b8a6)",
];

interface RowShape {
  monthLabel: string;
  monthsOut: number;
  [assetClass: string]: number | string;
}

function buildRows(
  points: GlidepathPointDTO[],
  assetClasses: string[],
): RowShape[] {
  return points.map((p) => {
    const row: RowShape = {
      monthLabel: p.date.length >= 7 ? p.date.slice(0, 7) : p.date,
      monthsOut: p.months_out,
    };
    for (const cls of assetClasses) {
      row[cls] = p.composition_pct_by_class[cls] ?? 0;
    }
    return row;
  });
}

function GlidepathTooltip(props: GlidepathTooltipProps) {
  const { active, payload, label } = props;
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div className="rounded-md border border-border/60 bg-popover text-popover-foreground text-xs shadow p-2 max-w-xs">
      <p className="font-semibold mb-1">{label}</p>
      <ul className="flex flex-col gap-0.5">
        {payload.map((entry, i) => (
          <li key={`${entry.name}-${i}`} className="flex items-baseline gap-2">
            <span
              className="w-2 h-2 rounded-sm inline-block"
              style={{ backgroundColor: entry.color }}
            />
            <span className="flex-1 truncate" title={String(entry.name)}>
              {String(entry.name)}
            </span>
            <span className="font-mono">
              {typeof entry.value === "number" ? entry.value.toFixed(1) : "—"}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function AllocationGlidepathChart({
  response,
}: AllocationGlidepathChartProps) {
  const rows = useMemo(
    () =>
      response
        ? buildRows(response.points, response.asset_classes)
        : [],
    [response],
  );

  if (response == null) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Allocation glidepath</CardTitle>
          <CardDescription>
            No current plan — glidepath unavailable.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const hasPoints = rows.length > 0;
  const hasCollapsed = response.collapsed_waypoints.length > 0;
  const excludedCount = response.excluded_targets.length;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Allocation glidepath</CardTitle>
        <CardDescription>
          Projected portfolio composition over time. Each band is one
          asset class the plan has a percentage-of-portfolio (or
          percentage-of-liquid) target on. Today&apos;s value comes from
          your latest snapshot; future values are linear-interpolated
          between waypoint dates set by the plan&apos;s targets.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {hasPoints ? (
          <ResponsiveContainer width="100%" height={280}>
            <AreaChart
              data={rows}
              margin={{ top: 4, right: 12, bottom: 4, left: 4 }}
            >
              <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
              <XAxis dataKey="monthLabel" fontSize={11} minTickGap={20} />
              <YAxis
                domain={[0, 100]}
                fontSize={11}
                tickFormatter={(v) => `${v}%`}
              />
              <Tooltip content={GlidepathTooltip} />
              {response.asset_classes.map((cls, i) => (
                <Area
                  key={cls}
                  type="monotone"
                  dataKey={cls}
                  stackId="alloc"
                  stroke={BAND_COLORS[i % BAND_COLORS.length]}
                  fill={BAND_COLORS[i % BAND_COLORS.length]}
                  fillOpacity={0.35}
                  isAnimationActive={false}
                />
              ))}
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <p className="text-sm text-muted-foreground py-4 text-center">
            The current plan has no allocation targets in
            <code className="font-mono mx-1">pct_of_portfolio</code>
            or
            <code className="font-mono mx-1">pct_of_liquid</code>
            units to plot.
          </p>
        )}

        {hasCollapsed ? (
          <div className="rounded-md border border-warning/40 bg-warning/10 p-2.5 text-xs flex flex-col gap-1">
            <p className="font-semibold">
              {response.collapsed_waypoints.length} waypoint
              {response.collapsed_waypoints.length === 1 ? "" : "s"} skipped
              (direction reversal)
            </p>
            <ul className="flex flex-col gap-0.5 text-muted-foreground">
              {response.collapsed_waypoints.map((w, i) => (
                <li key={`${w.asset_class}-${i}`}>
                  <span className="font-mono">{w.asset_class}</span> @{" "}
                  {w.waypoint_date.slice(0, 10)} →{" "}
                  {w.target_pct.toFixed(1)}% ({w.source_horizon}). {w.reason}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {response.anchor_status && response.anchor_status.length > 0 ? (
          <div className="text-xs text-muted-foreground border-t border-border/40 pt-2">
            <p className="font-semibold mb-1">Today&apos;s anchor per band</p>
            <ul className="flex flex-col gap-0.5">
              {response.anchor_status.map((a) => (
                <li key={a.asset_class}>
                  <span className="font-mono">{a.asset_class}</span> →{" "}
                  {a.matched ? (
                    <>
                      anchored at{" "}
                      <span className="font-mono">
                        {a.today_value.toFixed(1)}%
                      </span>
                      {a.alias_source ? (
                        <span className="opacity-70"> (via {a.alias_source})</span>
                      ) : null}
                    </>
                  ) : (
                    <span className="text-warning">
                      no snapshot match — anchored at 0% (chart shows the
                      target&apos;s direction only)
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {excludedCount > 0 ? (
          <p className="text-xs text-muted-foreground">
            {excludedCount} target
            {excludedCount === 1 ? "" : "s"} use non-percentage units
            (USD / NIS / shares / months / ratios) — they appear in the
            Actions Timeline below instead of this chart.
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
