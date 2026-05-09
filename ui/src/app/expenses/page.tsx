"use client";

import { useEffect, useState } from "react";

import { AnomalyHighlights } from "@/components/expenses/anomaly-highlights";
import { CategoryDonut } from "@/components/expenses/category-donut";
import { HeroStats } from "@/components/expenses/hero-stats";
import { MonthlySpendChart } from "@/components/expenses/monthly-spend-chart";
import { SourcesHealthTable } from "@/components/expenses/sources-health-table";
import { TopMerchantsCard } from "@/components/expenses/top-merchants-card";
import { Card, CardContent } from "@/components/ui/card";
import { expensesApi, type DashboardOverview } from "@/lib/expenses/api";
import { useFxMode } from "@/lib/expenses/fx-mode";

const USER_ID = "ariel";

export default function ExpensesOverviewPage() {
  const [data, setData] = useState<DashboardOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fxMode] = useFxMode();

  useEffect(() => {
    setLoading(true);
    expensesApi
      .dashboardOverview(USER_ID, 12, fxMode)
      .then(setData)
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [fxMode]);

  if (error) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-rose-600 text-sm">
          Failed to load: {error}
        </CardContent>
      </Card>
    );
  }
  if (loading || !data) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground text-sm">
          Loading dashboard…
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <HeroStats overview={data} fxMode={fxMode} />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MonthlySpendChart data={data.months} fxMode={fxMode} />
        <CategoryDonut data={data.current_month_top_categories} />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TopMerchantsCard data={data.top_merchants_current_month} />
        <AnomalyHighlights anomalies={data.anomalies} />
      </div>
      <SourcesHealthTable data={data.sources_health} />
    </div>
  );
}
