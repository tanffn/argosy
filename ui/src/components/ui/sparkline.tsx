"use client";

import * as React from "react";
import { Area, AreaChart, ResponsiveContainer } from "recharts";

import { cn } from "@/lib/utils";

export type SparklineTone = "success" | "warning" | "error" | "neutral" | "accent";

export interface SparklineProps {
  /** Numeric series. If empty, renders a flat zero line. */
  data: number[];
  /** Container width — defaults to 100% of parent (responsive). */
  width?: number | string;
  /** Container height in pixels. Default 36. */
  height?: number;
  /** Color tone for the area + stroke. */
  tone?: SparklineTone;
  className?: string;
  /** Optional aria-label for screen readers. */
  ariaLabel?: string;
}

const TONES: Record<SparklineTone, { stroke: string; fill: string }> = {
  // emerald-400 / emerald-500
  success: { stroke: "#34d399", fill: "#10b981" },
  // amber-400 / amber-500
  warning: { stroke: "#fbbf24", fill: "#f59e0b" },
  // red-400 / red-500
  error: { stroke: "#f87171", fill: "#ef4444" },
  // muted (neutral grey)
  neutral: { stroke: "#9ca3af", fill: "#6b7280" },
  // cyan-400 / cyan-500
  accent: { stroke: "#22d3ee", fill: "#06b6d4" },
};

/**
 * Tiny inline sparkline. No axes, no labels — purely decorative trend.
 *
 * Always renders something even with empty/length-1 data. Pads short
 * series so Recharts has at least two points (otherwise the AreaChart
 * collapses to nothing).
 */
export function Sparkline({
  data,
  width,
  height = 36,
  tone = "neutral",
  className,
  ariaLabel,
}: SparklineProps) {
  // Pad / sanitize so Recharts always has at least 2 points to draw a path.
  const safe = React.useMemo(() => {
    const cleaned = (data ?? []).filter((v) => Number.isFinite(v));
    if (cleaned.length === 0) return [0, 0];
    if (cleaned.length === 1) return [cleaned[0], cleaned[0]];
    return cleaned;
  }, [data]);

  const series = React.useMemo(
    () => safe.map((v, i) => ({ i, v })),
    [safe],
  );

  const colors = TONES[tone];
  const gradientId = React.useId();

  return (
    <div
      className={cn("w-full", className)}
      style={{ height, width: width ?? "100%" }}
      aria-label={ariaLabel}
      role={ariaLabel ? "img" : undefined}
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart
          data={series}
          margin={{ top: 2, right: 0, bottom: 2, left: 0 }}
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={colors.fill} stopOpacity={0.4} />
              <stop offset="100%" stopColor={colors.fill} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="v"
            stroke={colors.stroke}
            strokeWidth={1.5}
            fill={`url(#${gradientId})`}
            isAnimationActive={false}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
