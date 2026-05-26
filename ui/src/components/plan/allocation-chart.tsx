"use client";

import { useMemo } from "react";
import {
  Bar,
  BarChart,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { DraftResponse, PortfolioSnapshotDTO } from "@/lib/api";

interface AllocationChartProps {
  snapshot: PortfolioSnapshotDTO | null;
  draft: DraftResponse;
}

interface WeightTarget {
  label: string;
  target_pct: number;
  horizon: "long" | "medium" | "short";
}

// Pull explicit-weight targets (unit ~ "pct_of_portfolio") out of each horizon.
function extractWeightTargets(d: DraftResponse): WeightTarget[] {
  const out: WeightTarget[] = [];
  const horizons: Array<["long" | "medium" | "short", typeof d.horizon_long]> = [
    ["long", d.horizon_long],
    ["medium", d.horizon_medium],
    ["short", d.horizon_short],
  ];
  for (const [horizon, h] of horizons) {
    if (!h) continue;
    for (const t of h.targets) {
      if (!t || typeof t !== "object") continue;
      const row = t as Record<string, unknown>;
      const unit = (row.unit as string | undefined)?.toLowerCase() ?? "";
      const value = row.value;
      const label = (row.label as string | undefined) ?? "";
      if (
        (unit.includes("pct_of_portfolio") || unit.includes("pct_of_net_worth")) &&
        typeof value === "number" &&
        label
      ) {
        out.push({ label, target_pct: value, horizon });
      }
    }
  }
  return out;
}

// Bucket positions into coarse categories for the bar chart. We collapse on
// asset_type since the TSV's `details` field is too granular to chart cleanly.
function bucketPositions(snapshot: PortfolioSnapshotDTO): {
  category: string;
  usd_value_k: number;
  pct: number;
}[] {
  const buckets = new Map<string, number>();
  for (const p of snapshot.positions) {
    if (!p.usd_value_k) continue;
    const key = (p.asset_type || p.details || "other").trim() || "other";
    buckets.set(key, (buckets.get(key) ?? 0) + p.usd_value_k);
  }
  const total = Array.from(buckets.values()).reduce((s, v) => s + v, 0);
  if (total <= 0) return [];
  return Array.from(buckets.entries())
    .map(([category, usd_value_k]) => ({
      category,
      usd_value_k,
      pct: (usd_value_k / total) * 100,
    }))
    .sort((a, b) => b.usd_value_k - a.usd_value_k);
}

// Pleasant palette for the bars.
const BAR_COLORS = [
  "var(--color-chart-1, #6366f1)",
  "var(--color-chart-2, #22d3ee)",
  "var(--color-chart-3, #f59e0b)",
  "var(--color-chart-4, #10b981)",
  "var(--color-chart-5, #f43f5e)",
  "var(--color-chart-6, #8b5cf6)",
  "var(--color-chart-7, #14b8a6)",
];

export function AllocationChart(props: AllocationChartProps) {
  const { snapshot, draft } = props;

  const bars = useMemo(
    () => (snapshot ? bucketPositions(snapshot) : []),
    [snapshot],
  );
  const weightTargets = useMemo(() => extractWeightTargets(draft), [draft]);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Current allocation</CardTitle>
        <CardDescription>
          {snapshot?.snapshot_date
            ? `from Family Finances Status ${snapshot.snapshot_date}`
            : "no portfolio snapshot found"}
          {snapshot?.total_usd_value_k
            ? ` · total $${(snapshot.total_usd_value_k * 1000).toLocaleString()}`
            : ""}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {bars.length === 0 ? (
          <p className="text-sm text-muted-foreground py-8 text-center">
            Upload a Family Finances Status TSV to see your allocation.
          </p>
        ) : (
          <div className="flex flex-col gap-4">
            <ResponsiveContainer width="100%" height={Math.max(180, bars.length * 32)}>
              <BarChart
                data={bars}
                layout="vertical"
                margin={{ top: 4, right: 30, bottom: 4, left: 4 }}
              >
                <XAxis
                  type="number"
                  tickFormatter={(v) => `${v.toFixed(0)}%`}
                  domain={[0, 100]}
                  fontSize={11}
                />
                <YAxis
                  type="category"
                  dataKey="category"
                  width={140}
                  fontSize={11}
                />
                <Tooltip
                  cursor={false}
                  formatter={((value: number) => [
                    `${value.toFixed(1)}%`,
                    "current",
                  ]) as unknown as never}
                />
                <Bar dataKey="pct" isAnimationActive={false}>
                  {bars.map((b, i) => (
                    <Cell key={b.category} fill={BAR_COLORS[i % BAR_COLORS.length]} />
                  ))}
                </Bar>
                {/* Render plan-proposed weight targets as orange reference
                    lines. Each target is plotted at its target pct so the
                    line crosses any bar that should converge toward it. */}
                {weightTargets.map((t, i) => (
                  <ReferenceLine
                    key={`tgt-${i}`}
                    x={t.target_pct}
                    stroke="#f97316"
                    strokeDasharray="4 4"
                    label={{
                      value: `${t.target_pct.toFixed(0)}% (${t.horizon})`,
                      position: "top",
                      fill: "#f97316",
                      fontSize: 10,
                    }}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
            {weightTargets.length > 0 && (
              <div className="border-t border-border/40 pt-3">
                <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-2">
                  Plan-proposed weight targets ({weightTargets.length})
                </div>
                <ul className="flex flex-col gap-1.5 text-xs">
                  {weightTargets.map((t, i) => (
                    <li key={i} className="flex items-baseline gap-2">
                      <span className="font-mono text-orange-500">
                        {t.target_pct.toFixed(1)}%
                      </span>
                      <span className="text-muted-foreground text-[10px] uppercase">
                        {t.horizon}
                      </span>
                      <span className="flex-1">{t.label}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {weightTargets.length === 0 && (
              <p className="text-xs text-muted-foreground">
                Draft contains no explicit pct-of-portfolio weight targets to overlay.
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
