"use client";

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { WealthRetirementBlock } from "@/lib/api";

interface WealthTrajectoryChartProps {
  retirement: WealthRetirementBlock;
}

/**
 * Wealth-trajectory line chart (3 scenarios × 25 years × target ref line).
 *
 * Three series:
 *   - bear (0% real_return)
 *   - conservative (2%)
 *   - typical (4.5%)
 *
 * One horizontal reference line at ``target_portfolio_nis`` (the
 * FIRE target = annual_expenses / SWR). The intersection point of
 * each line with the target visualises the projected retirement age
 * (already echoed in the retirement-card scenario tiles).
 *
 * Uses the same Recharts pattern as ``components/plan/allocation-chart.tsx``.
 */
export function WealthTrajectoryChart({ retirement }: WealthTrajectoryChartProps) {
  const data = retirement.trajectory;
  const target = retirement.target_portfolio_nis;

  if (data.length === 0) {
    return (
      <div className="text-sm text-muted-foreground py-12 text-center">
        Insufficient data to draw a wealth trajectory.
      </div>
    );
  }

  // Format axis ticks compactly so the chart stays readable at 25-year
  // horizons.
  const formatMillions = (v: number) => {
    if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
    if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(0)}k`;
    return v.toFixed(0);
  };

  return (
    <ResponsiveContainer width="100%" height={280}>
      <LineChart
        data={data}
        margin={{ top: 8, right: 16, bottom: 8, left: 8 }}
      >
        <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" />
        <XAxis
          dataKey="year"
          type="number"
          domain={[0, "dataMax"]}
          fontSize={11}
          tickFormatter={(v) => `${v}y`}
          label={{
            value: "years from now",
            position: "insideBottom",
            offset: -4,
            fontSize: 10,
            fill: "var(--color-muted-foreground)",
          }}
        />
        <YAxis
          tickFormatter={formatMillions}
          fontSize={11}
          width={56}
          label={{
            value: "NIS",
            angle: -90,
            position: "insideLeft",
            fontSize: 10,
            fill: "var(--color-muted-foreground)",
          }}
        />
        <Tooltip
          formatter={
            ((value: number, key: string) => [
              `${formatMillions(value)} NIS`,
              key,
            ]) as unknown as never
          }
          labelFormatter={(label) => `Year ${label}`}
          contentStyle={{
            background: "var(--color-popover)",
            border: "1px solid var(--color-border)",
            fontSize: 12,
          }}
        />
        <Legend
          verticalAlign="top"
          height={24}
          iconType="line"
          wrapperStyle={{ fontSize: 11 }}
        />
        {target !== null && target > 0 && (
          <ReferenceLine
            y={target}
            stroke="var(--color-warning, #f59e0b)"
            strokeDasharray="4 4"
            label={{
              value: `FIRE target ${formatMillions(target)}`,
              position: "insideTopRight",
              fill: "var(--color-warning, #f59e0b)",
              fontSize: 10,
            }}
          />
        )}
        <Line
          type="monotone"
          dataKey="bear"
          name="bear (0%)"
          stroke="var(--color-error, #ef4444)"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="conservative"
          name="conservative (2%)"
          stroke="var(--color-info, #3b82f6)"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="typical"
          name="typical (4.5%)"
          stroke="var(--color-success, #10b981)"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
