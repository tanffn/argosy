"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  type DashboardOverview,
  type CategorySpend,
} from "@/lib/expenses/api";
import { formatNIS, formatPercent, formatRelativeMonth } from "@/lib/expenses/format";
import { type FxMode } from "@/lib/expenses/fx-mode";

interface HeroStatsProps {
  overview: DashboardOverview;
  fxMode: FxMode;
}

function monthSpend(month: { totals_by_currency: Record<string, number> }, fxMode: FxMode): number {
  if (fxMode === "nis") {
    // Best-effort: sum only NIS for now; v1 doesn't FX-convert client-side.
    return month.totals_by_currency.NIS ?? 0;
  }
  // Per-currency mode: NIS-only "primary" total + foreign rendered separately elsewhere.
  return month.totals_by_currency.NIS ?? 0;
}

export function HeroStats({ overview, fxMode }: HeroStatsProps) {
  const months = overview.months;
  const cur = months.at(-1);
  const prev = months.at(-2);
  const curNis = cur ? monthSpend(cur, fxMode) : 0;
  const prevNis = prev ? monthSpend(prev, fxMode) : 0;
  const trend = prevNis > 0 ? ((curNis - prevNis) / prevNis) * 100 : 0;
  const top: CategorySpend | undefined = overview.current_month_top_categories[0];
  const sources = overview.sources_health;
  const refundsCount = 0; // TODO: surface in API; for v1, hide if 0.

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            {cur ? formatRelativeMonth(cur.month) : "This month"}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-semibold">{formatNIS(curNis)}</div>
          {prev && (
            <div className={trend > 0 ? "text-xs text-rose-600" : "text-xs text-emerald-600"}>
              {trend > 0 ? "+" : ""}{trend.toFixed(1)}% vs last month
            </div>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Top category
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-semibold capitalize">{top?.label_en ?? "—"}</div>
          {top && (
            <div className="text-xs text-muted-foreground">
              {formatNIS(top.total_nis)} · {formatPercent(top.percent)}
            </div>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Sources
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-semibold">{sources.length}</div>
          <div className="text-xs text-muted-foreground">
            {sources.filter((s) => s.status === "green").length} reconciled
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
