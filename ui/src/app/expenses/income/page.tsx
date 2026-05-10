"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { expensesApi, type IncomeBreakdown } from "@/lib/expenses/api";
import { colorForSlug, formatMonth, formatNIS } from "@/lib/expenses/format";

const USER_ID = "ariel";

function currentYYYYMM(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function IncomeDrilldownInner() {
  const router = useRouter();
  const params = useSearchParams();
  const monthParam = params.get("month");
  const month =
    monthParam && /^\d{4}-\d{2}$/.test(monthParam) ? monthParam : currentYYYYMM();

  const [data, setData] = useState<IncomeBreakdown | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    expensesApi
      .incomeBreakdown(USER_ID, month)
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setError(null);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(String(e));
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, [month]);

  function shiftMonth(delta: number) {
    const [y, m] = month.split("-").map(Number);
    const dt = new Date(y, m - 1 + delta, 1);
    const next = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}`;
    const qs = new URLSearchParams(params.toString());
    qs.set("month", next);
    router.replace(`/expenses/income?${qs.toString()}`);
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
          Loading income…
        </CardContent>
      </Card>
    );
  }

  const total = data.total_nis;
  const totalBase = total || 1;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold">
            Income — {formatMonth(data.month)}
          </h2>
          <div className="text-sm text-muted-foreground">
            Real income only — refunds (money-back-from-mistakes) are tracked separately.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => shiftMonth(-1)}
            className="rounded border border-border px-2 py-1 text-xs hover:bg-secondary/40"
          >
            ← Prev
          </button>
          <button
            onClick={() => shiftMonth(1)}
            className="rounded border border-border px-2 py-1 text-xs hover:bg-secondary/40"
          >
            Next →
          </button>
        </div>
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Total income
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-3xl font-semibold text-emerald-600">
            {formatNIS(total)}
          </div>
          {data.by_category.length > 0 && (
            <div className="mt-3">
              <div className="flex h-3 w-full overflow-hidden rounded">
                {data.by_category.map((c) => (
                  <div
                    key={c.slug}
                    title={`${c.label_en}: ${formatNIS(c.total_nis)}`}
                    style={{
                      width: `${(c.total_nis / totalBase) * 100}%`,
                      backgroundColor: colorForSlug(c.slug),
                    }}
                  />
                ))}
              </div>
            </div>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium">By stream</CardTitle>
        </CardHeader>
        <CardContent>
          {data.by_category.length === 0 ? (
            <div className="text-sm text-muted-foreground py-3">
              No income recorded for this month.
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-muted-foreground border-b border-border">
                  <th className="text-left py-2 pr-2">Category</th>
                  <th className="text-right py-2 px-2">Amount</th>
                  <th className="text-right py-2 px-2">% of total</th>
                  <th className="text-right py-2 pl-2">Tx</th>
                </tr>
              </thead>
              <tbody>
                {data.by_category.map((c) => (
                  <tr key={c.slug} className="border-b border-border/60">
                    <td className="py-2 pr-2">
                      <span className="inline-flex items-center gap-2">
                        <span
                          className="h-2 w-2 rounded-full"
                          style={{ backgroundColor: colorForSlug(c.slug) }}
                        />
                        <span>{c.label_en}</span>
                      </span>
                    </td>
                    <td className="py-2 px-2 text-right tabular-nums text-emerald-600">
                      {formatNIS(c.total_nis)}
                    </td>
                    <td className="py-2 px-2 text-right tabular-nums text-muted-foreground">
                      {c.percent.toFixed(0)}%
                    </td>
                    <td className="py-2 pl-2 text-right tabular-nums">
                      {c.transaction_count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium">Transactions</CardTitle>
        </CardHeader>
        <CardContent>
          {data.transactions.length === 0 ? (
            <div className="text-sm text-muted-foreground py-3">
              No income transactions for this month.
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-muted-foreground border-b border-border">
                  <th className="text-left py-2 pr-2">Date</th>
                  <th className="text-left py-2 px-2">Merchant</th>
                  <th className="text-left py-2 px-2">Category</th>
                  <th className="text-right py-2 pl-2">Amount</th>
                </tr>
              </thead>
              <tbody>
                {data.transactions.map((t) => (
                  <tr key={t.id} className="border-b border-border/60">
                    <td className="py-2 pr-2 tabular-nums whitespace-nowrap text-muted-foreground">
                      {t.occurred_on}
                    </td>
                    <td className="py-2 px-2">{t.merchant_raw}</td>
                    <td className="py-2 px-2 text-xs text-muted-foreground">
                      {t.category_slug ?? "—"}
                    </td>
                    <td className="py-2 pl-2 text-right tabular-nums text-emerald-600">
                      +{t.amount_nis !== null ? formatNIS(t.amount_nis) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export default function IncomeDrilldownPage() {
  return (
    <Suspense
      fallback={
        <Card>
          <CardContent className="py-8 text-center text-muted-foreground text-sm">
            Loading…
          </CardContent>
        </Card>
      }
    >
      <IncomeDrilldownInner />
    </Suspense>
  );
}
