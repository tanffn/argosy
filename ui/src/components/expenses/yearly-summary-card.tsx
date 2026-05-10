"use client";

import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type YearlySummary } from "@/lib/expenses/api";
import { colorForSlug, formatNIS, formatPercent } from "@/lib/expenses/format";

interface YearlySummaryCardProps {
  data: YearlySummary;
}

/**
 * "Bottom line" card — the user's headline ask: "in a year you spend X on…
 * this month you spend Y". Surfaces total NIS spend over the last 12 months,
 * average per month, top 5 categories with percent, and a current-vs-avg
 * comparison so the current month is contextualized.
 */
export function YearlySummaryCard({ data }: YearlySummaryCardProps) {
  if (data.months_covered === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Bottom line — last 12 months</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-sm text-muted-foreground py-4 text-center">
            Not enough data yet for a yearly rollup.
          </div>
        </CardContent>
      </Card>
    );
  }

  const subtitle =
    data.months_covered < 12
      ? `Based on ${data.months_covered} months of data`
      : "Based on the last 12 months";

  const trendLabel =
    data.current_vs_avg_pct === null
      ? null
      : data.current_vs_avg_pct >= 0
        ? `+${data.current_vs_avg_pct.toFixed(0)}% vs avg`
        : `${data.current_vs_avg_pct.toFixed(0)}% vs avg`;
  const trendColor =
    data.current_vs_avg_pct === null
      ? ""
      : data.current_vs_avg_pct > 5
        ? "text-rose-600"
        : data.current_vs_avg_pct < -5
          ? "text-emerald-600"
          : "text-muted-foreground";

  // Bar widths normalized to the largest top-category total, so the user can
  // see relative weight at a glance.
  const maxCat = Math.max(
    1,
    ...data.top_categories_12m.map((c) => c.total_nis),
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center justify-between gap-2">
          <span>Bottom line — last 12 months</span>
          <span className="text-xs font-normal text-muted-foreground">
            {subtitle}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div>
            <div className="text-xs text-muted-foreground">Total spent</div>
            <div className="text-2xl font-semibold tabular-nums">
              {formatNIS(data.yearly_spending_total_nis)}
            </div>
            <div className="text-xs text-muted-foreground">
              Outflow only — excludes salary, transfers, investments
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Total income</div>
            <div className="text-2xl font-semibold tabular-nums text-emerald-600">
              {formatNIS(data.yearly_inflow_total_nis)}
            </div>
            <div className="text-xs text-muted-foreground">
              Salary, RSU, refunds, dividends
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">
              Spent per month, on avg
            </div>
            <div className="text-2xl font-semibold tabular-nums">
              {formatNIS(data.avg_per_month_nis)}
            </div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">
              This month vs avg
            </div>
            <div className={`text-2xl font-semibold tabular-nums ${trendColor}`}>
              {trendLabel ?? "—"}
            </div>
          </div>
        </div>

        {data.top_categories_12m.length > 0 && (
          <div className="mt-5">
            <div className="text-xs text-muted-foreground mb-2">
              Where it goes — top 5 spending categories (12-month)
            </div>
            <ul className="flex flex-col gap-1.5">
              {data.top_categories_12m.map((c) => {
                const pctOfMax = (c.total_nis / maxCat) * 100;
                return (
                  <li key={c.slug}>
                    <Link
                      href={`/expenses/transactions?category=${encodeURIComponent(c.slug)}`}
                      className="block group"
                    >
                      <div className="flex items-baseline gap-2 text-sm">
                        <span className="capitalize flex-1 min-w-0 truncate group-hover:underline">
                          {c.label_en}
                        </span>
                        <span className="tabular-nums text-muted-foreground text-xs">
                          {formatPercent(c.percent)}
                        </span>
                        <span className="tabular-nums w-24 text-right">
                          {formatNIS(c.total_nis)}
                        </span>
                      </div>
                      <div className="h-1.5 w-full bg-secondary/40 rounded-full overflow-hidden mt-1">
                        <div
                          className="h-full rounded-full transition-all"
                          style={{
                            width: `${pctOfMax}%`,
                            backgroundColor: colorForSlug(c.slug),
                          }}
                        />
                      </div>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
