"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { AnomalyHighlights } from "@/components/expenses/anomaly-highlights";
import { CategoryDonut } from "@/components/expenses/category-donut";
import { DividendsCard } from "@/components/expenses/dividends-card";
import { HeroStats } from "@/components/expenses/hero-stats";
import { MonthlySpendChart } from "@/components/expenses/monthly-spend-chart";
import { MonthPicker } from "@/components/expenses/month-picker";
import { SourcesHealthTable } from "@/components/expenses/sources-health-table";
import { TaxesCard } from "@/components/expenses/taxes-card";
import { TopMerchantsCard } from "@/components/expenses/top-merchants-card";
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
  // null = use latest month with data; otherwise 'YYYY-MM'.
  const monthParam = params.get("month");
  const selectedMonth =
    monthParam && /^\d{4}-\d{2}$/.test(monthParam) ? monthParam : null;
  const windowParam = params.get("window");
  const selectedWindow: YearlyWindow =
    windowParam === "calendar_year" ? "calendar_year" : "trailing_12";

  useEffect(() => {
    setLoading(true);
    expensesApi
      .dashboardOverview(USER_ID, 12, fxMode, selectedMonth, selectedWindow)
      .then(setData)
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [fxMode, selectedMonth, selectedWindow]);

  function setMonth(m: string | null) {
    const next = new URLSearchParams(params.toString());
    if (m === null) next.delete("month");
    else next.set("month", m);
    const qs = next.toString();
    router.replace(qs ? `/expenses?${qs}` : "/expenses");
    if (m !== null && typeof window !== "undefined") {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

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

  const focal = data.current_month;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
        <MonthPicker
          months={data.months}
          value={selectedMonth}
          onChange={setMonth}
        />
      </div>
      <YearlySummaryCard
        data={data.yearly_summary}
        onWindowChange={setWindow}
      />
      <HeroStats overview={data} fxMode={fxMode} />
      {(data.dividends || data.taxes) && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {data.dividends && <DividendsCard data={data.dividends} />}
          {data.taxes && <TaxesCard data={data.taxes} />}
        </div>
      )}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MonthlySpendChart data={data.months} fxMode={fxMode} />
        <CategoryDonut data={data.current_month_top_categories} month={focal} />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TopMerchantsCard data={data.top_merchants_current_month} month={focal} />
        <AnomalyHighlights anomalies={data.anomalies} />
      </div>
      <SourcesHealthTable data={data.sources_health} />
    </div>
  );
}

export default function ExpensesOverviewPage() {
  return (
    <Suspense fallback={<Card><CardContent className="py-8 text-center text-muted-foreground text-sm">Loading…</CardContent></Card>}>
      <ExpensesOverviewInner />
    </Suspense>
  );
}
