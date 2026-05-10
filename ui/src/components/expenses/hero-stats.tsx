"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type DashboardOverview } from "@/lib/expenses/api";
import { formatMonth, formatNIS } from "@/lib/expenses/format";
import { type FxMode } from "@/lib/expenses/fx-mode";

interface HeroStatsProps {
  overview: DashboardOverview;
  // fxMode is unused for the hero scalars now — they're already split by the
  // server into spending/inflow NIS-only — but we accept it so the parent
  // signature stays uniform with the chart sibling.
  fxMode: FxMode;
}

export function HeroStats({ overview }: HeroStatsProps) {
  const monthLabel = overview.current_month
    ? formatMonth(overview.current_month)
    : "—";
  const spending = overview.current_month_spending_nis;
  const inflow = overview.current_month_inflow_nis;
  const avg = overview.yearly_summary.avg_per_month_nis;
  const trendPct = overview.yearly_summary.current_vs_avg_pct;
  const sources = overview.sources_health;
  const reconciled = sources.filter((s) => s.status === "green").length;
  const inflowCount = overview.current_month_inflow.length;

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
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
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Money in — {monthLabel}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-semibold text-emerald-600">
            {formatNIS(inflow)}
          </div>
          <div className="text-xs text-muted-foreground">
            {inflowCount > 0
              ? `${inflowCount} income source${inflowCount === 1 ? "" : "s"} (salary, RSU, refunds…)`
              : "No income credited this month"}
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Statements reconciled
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-semibold">
            {reconciled}/{sources.length}
          </div>
          <div className="text-xs text-muted-foreground">
            Cards/banks fully matched against parsed totals
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Anomalies
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-semibold">
            {overview.anomalies.length}
          </div>
          <div className="text-xs text-muted-foreground">
            {overview.anomalies.filter((a) => a.severity === "red").length} red ·{" "}
            {overview.anomalies.filter((a) => a.severity === "yellow").length} yellow
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
