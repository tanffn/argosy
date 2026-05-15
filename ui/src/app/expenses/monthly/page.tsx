"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { AnomalyHighlights } from "@/components/expenses/anomaly-highlights";
import { CategoriesVsTypicalCard } from "@/components/expenses/categories-vs-typical-card";
import { CategoryDonut } from "@/components/expenses/category-donut";
import { HeroStats } from "@/components/expenses/hero-stats";
import { LargestTransactionsCard } from "@/components/expenses/largest-transactions-card";
import { MonthlySpendChart } from "@/components/expenses/monthly-spend-chart";
import { MonthPicker } from "@/components/expenses/month-picker";
import { TopMerchantsCard } from "@/components/expenses/top-merchants-card";
import { Card, CardContent } from "@/components/ui/card";
import { expensesApi, type DashboardMonthly } from "@/lib/expenses/api";
import { useFxMode } from "@/lib/expenses/fx-mode";

const USER_ID = "ariel";

function ExpensesMonthlyInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [data, setData] = useState<DashboardMonthly | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fxMode] = useFxMode();

  const monthParam = params.get("month");
  const selectedMonth = monthParam && /^\d{4}-\d{2}$/.test(monthParam) ? monthParam : null;

  useEffect(() => {
    if (!selectedMonth) {
      const today = new Date();
      const ym = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}`;
      expensesApi.dashboardMonthly(USER_ID, ym, fxMode)
        .then((d) => {
          const latest = d.available_months[d.available_months.length - 1];
          if (latest && latest !== ym) {
            router.replace(`/expenses/monthly?month=${latest}`);
            return;
          }
          setData(d);
        })
        .catch((e: unknown) => setError(String(e)))
        .finally(() => setLoading(false));
      return;
    }
    setLoading(true);
    expensesApi.dashboardMonthly(USER_ID, selectedMonth, fxMode)
      .then(setData)
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [fxMode, selectedMonth, router]);

  function setMonth(m: string) {
    const next = new URLSearchParams(params.toString());
    next.set("month", m);
    router.replace(`/expenses/monthly?${next.toString()}`);
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
          Loading…
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
        <MonthPicker
          months={data.available_months.map((m) => ({
            month: m,
            totals_by_currency: {},
            transaction_count: 0,
          }))}
          value={data.month}
          onChange={(m) => m && setMonth(m)}
        />
      </div>
      <HeroStats mode="monthly" data={data} />
      <MonthlySpendChart
        mode="focal"
        chartWindow={data.chart_window}
        onMonthSelected={setMonth}
      />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <CategoryDonut data={data.top_categories} month={data.month} />
        <CategoriesVsTypicalCard data={data.categories_vs_typical} month={data.month} />
      </div>
      {data.oneoff_categories.length > 0 && (
        <CategoryDonut
          data={data.oneoff_categories}
          month={data.month}
          title={`One-off / vacation expenses — ${data.month}`}
        />
      )}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TopMerchantsCard data={data.top_merchants} month={data.month} />
        <LargestTransactionsCard transactions={data.largest_transactions} month={data.month} />
      </div>
      <AnomalyHighlights anomalies={data.anomalies} />
    </div>
  );
}

export default function ExpensesMonthlyPage() {
  return (
    <Suspense fallback={
      <Card><CardContent className="py-8 text-center text-muted-foreground text-sm">Loading…</CardContent></Card>
    }>
      <ExpensesMonthlyInner />
    </Suspense>
  );
}
