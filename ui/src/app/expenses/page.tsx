"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { CurrencyMixCard } from "@/components/expenses/currency-mix-card";
import { DividendsCard } from "@/components/expenses/dividends-card";
import { HeroStats } from "@/components/expenses/hero-stats";
import { MonthlySpendChart } from "@/components/expenses/monthly-spend-chart";
import { SavingsRateTrend } from "@/components/expenses/savings-rate-trend";
import { SourcesHealthTable } from "@/components/expenses/sources-health-table";
import { TaxesCard } from "@/components/expenses/taxes-card";
import { TopMoversCard } from "@/components/expenses/top-movers-card";
import { YearlySummaryCard } from "@/components/expenses/yearly-summary-card";
import { Card, CardContent } from "@/components/ui/card";
import {
  expensesApi,
  type DashboardOverview,
  type YearlyWindow,
} from "@/lib/expenses/api";
import { useFxMode } from "@/lib/expenses/fx-mode";

const USER_ID = "ariel";

function ExpensesOverviewInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [data, setData] = useState<DashboardOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fxMode] = useFxMode();
  const windowParam = params.get("window");
  const selectedWindow: YearlyWindow =
    windowParam === "calendar_year" ? "calendar_year" : "trailing_12";

  useEffect(() => {
    setLoading(true);
    expensesApi
      .dashboardOverview(USER_ID, 12, fxMode, selectedWindow)
      .then(setData)
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [fxMode, selectedWindow]);

  function setWindow(w: YearlyWindow) {
    const next = new URLSearchParams(params.toString());
    if (w === "trailing_12") next.delete("window");
    else next.set("window", w);
    const qs = next.toString();
    router.replace(qs ? `/expenses?${qs}` : "/expenses");
  }

  if (error) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-error text-sm">
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
      <HeroStats mode="yearly" overview={data} />
      <YearlySummaryCard data={data.yearly_summary} onWindowChange={setWindow} />
      <SavingsRateTrend data={data.savings_rate_trend} />
      <TopMoversCard data={data.top_movers} />
      <CurrencyMixCard data={data.currency_mix} />
      {(data.dividends || data.taxes) && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {data.dividends && <DividendsCard data={data.dividends} />}
          {data.taxes && <TaxesCard data={data.taxes} />}
        </div>
      )}
      <MonthlySpendChart mode="small" data={data.months} />
      <SourcesHealthTable data={data.sources_health} />
    </div>
  );
}

export default function ExpensesOverviewPage() {
  return (
    <Suspense fallback={
      <Card><CardContent className="py-8 text-center text-muted-foreground text-sm">Loading…</CardContent></Card>
    }>
      <ExpensesOverviewInner />
    </Suspense>
  );
}
