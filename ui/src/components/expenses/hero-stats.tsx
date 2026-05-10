"use client";

import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type DashboardOverview } from "@/lib/expenses/api";
import { formatMonth, formatNIS } from "@/lib/expenses/format";
import { type FxMode } from "@/lib/expenses/fx-mode";

interface HeroStatsProps {
  overview: DashboardOverview;
  // fxMode is unused for the hero scalars now — they're already split by the
  // server into spending/income/refunds (NIS-only). Accepted to keep parent
  // signature uniform with the chart sibling.
  fxMode: FxMode;
}

export function HeroStats({ overview }: HeroStatsProps) {
  const monthLabel = overview.current_month
    ? formatMonth(overview.current_month)
    : "—";
  // Defensive defaults — older backend payloads pre-date the income/refund split.
  const spending = overview.current_month_spending_nis ?? 0;
  const income =
    overview.current_month_income_nis ??
    // Back-compat: if backend predates split, fall back to inflow (= income+refunds).
    overview.current_month_inflow_nis ?? 0;
  const refunds = overview.current_month_refunds_nis ?? 0;
  const avg = overview.yearly_summary?.avg_per_month_nis ?? 0;
  const trendPct = overview.yearly_summary?.current_vs_avg_pct ?? null;
  const sources = overview.sources_health ?? [];
  const reconciled = sources.filter((s) => s.status === "green").length;
  const incomeCount = (
    overview.current_month_income ?? overview.current_month_inflow ?? []
  ).length;
  const incomeHref = overview.current_month
    ? `/expenses/income?month=${overview.current_month}`
    : "/expenses/income";
  const showRefundCard = refunds > 0;

  // Top row: spent · income · (refunds, if any). Bottom row, tighter:
  // statements reconciled + anomalies.
  const topCols = showRefundCard ? "lg:grid-cols-3" : "lg:grid-cols-2";

  return (
    <div className="flex flex-col gap-3">
      <div className={`grid grid-cols-1 sm:grid-cols-2 ${topCols} gap-3`}>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Spent — {monthLabel}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">{formatNIS(spending)}</div>
            {avg > 0 && trendPct !== null ? (
              <div
                className={
                  trendPct > 5
                    ? "text-xs text-rose-600"
                    : trendPct < -5
                      ? "text-xs text-emerald-600"
                      : "text-xs text-muted-foreground"
                }
              >
                {trendPct > 0 ? "+" : ""}
                {trendPct.toFixed(0)}% vs 12-mo avg ({formatNIS(avg)})
              </div>
            ) : (
              <div className="text-xs text-muted-foreground">
                Outflow only — excludes salary, transfers, investments
              </div>
            )}
          </CardContent>
        </Card>
        <Link href={incomeHref} className="block group">
          <Card className="transition-colors group-hover:border-emerald-500/40">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">
                Income — {monthLabel}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-semibold text-emerald-600">
                {formatNIS(income)}
              </div>
              <div className="text-xs text-muted-foreground">
                {incomeCount > 0
                  ? `${incomeCount} stream${incomeCount === 1 ? "" : "s"} · click to drill into salary, RSU, dividends…`
                  : "No income credited this month"}
              </div>
            </CardContent>
          </Card>
        </Link>
        {showRefundCard && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground">
                Refunds — {monthLabel}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-2xl font-semibold text-sky-600">
                {formatNIS(refunds)}
              </div>
              <div className="text-xs text-muted-foreground">
                Money back on prior charges (not income)
              </div>
            </CardContent>
          </Card>
        )}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-xs font-medium text-muted-foreground">
              Statements reconciled
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <div className="text-base font-semibold">
              {reconciled}/{sources.length}
            </div>
            <div className="text-xs text-muted-foreground">
              Cards/banks fully matched
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-xs font-medium text-muted-foreground">
              Anomalies
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <div className="text-base font-semibold">
              {overview.anomalies.length}
            </div>
            <div className="text-xs text-muted-foreground">
              {overview.anomalies.filter((a) => a.severity === "red").length} red ·{" "}
              {overview.anomalies.filter((a) => a.severity === "yellow").length} yellow
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
