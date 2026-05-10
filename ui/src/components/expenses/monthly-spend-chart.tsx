"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type MonthlyTotalEntry } from "@/lib/expenses/api";
import { formatMonth, formatNIS } from "@/lib/expenses/format";
import { type FxMode } from "@/lib/expenses/fx-mode";

interface MonthlySpendChartProps {
  data: MonthlyTotalEntry[];
  fxMode: FxMode;
  height?: number;
  /** Currently selected/focal month — used by the header drilldown link
   *  so the user can jump to /expenses/transactions filtered to it. */
  selectedMonth?: string | null;
  /**
   * Click on a bar:
   * - if onMonthSelected is provided, calls it with the bar's month key
   *   (re-scopes the dashboard to that month — see ExpensesOverviewPage).
   * - otherwise, falls back to navigating to /expenses/transactions with
   *   from_date/to_date for that month, so the chart stays useful in any
   *   other context.
   */
  onMonthSelected?: (monthKey: string) => void;
}

const CURRENCY_COLORS: Record<string, string> = {
  NIS: "hsl(220, 70%, 55%)",
  USD: "hsl(160, 65%, 50%)",
  EUR: "hsl(280, 65%, 60%)",
  GBP: "hsl(30, 80%, 55%)",
};

interface ChartRow {
  month: string;        // pretty label, e.g. 'May 2026'
  monthKey: string;     // 'YYYY-MM'
  [currency: string]: number | string;
}

/** First/last day for the deep-link query string. Gracefully degrades to
 *  the input string if parsing fails. */
function firstAndLast(yyyymm: string): { from: string; to: string } | null {
  const [y, m] = yyyymm.split("-").map(Number);
  if (!y || !m) return null;
  const first = `${yyyymm}-01`;
  const lastDay = new Date(y, m, 0).getDate();
  const last = `${yyyymm}-${String(lastDay).padStart(2, "0")}`;
  return { from: first, to: last };
}

export function MonthlySpendChart({
  data, fxMode, height = 280,
  selectedMonth, onMonthSelected,
}: MonthlySpendChartProps) {
  const router = useRouter();

  // Build chart data: one row per month, columns per currency.
  const currencies = new Set<string>();
  for (const m of data) for (const c of Object.keys(m.totals_by_currency)) currencies.add(c);
  const ccyOrder = ["NIS", "USD", "EUR", "GBP"].filter((c) => currencies.has(c));
  for (const c of currencies) if (!ccyOrder.includes(c)) ccyOrder.push(c);

  const rows: ChartRow[] = data.map((m) => {
    const row: ChartRow = {
      month: formatMonth(m.month),
      monthKey: m.month,
    };
    for (const c of ccyOrder) row[c] = m.totals_by_currency[c] ?? 0;
    return row;
  });

  const handleBarClick = (payload: { payload?: ChartRow }) => {
    const monthKey = payload?.payload?.monthKey;
    if (!monthKey) return;
    if (onMonthSelected) {
      onMonthSelected(monthKey);
      return;
    }
    // Fallback: drill to transactions filtered to that month.
    const range = firstAndLast(monthKey);
    if (!range) return;
    router.push(
      `/expenses/transactions?from_date=${range.from}&to_date=${range.to}`,
    );
  };

  const drillMonth = selectedMonth ?? data[data.length - 1]?.month ?? null;
  const drillRange = drillMonth ? firstAndLast(drillMonth) : null;
  const drillHref = drillRange
    ? `/expenses/transactions?from_date=${drillRange.from}&to_date=${drillRange.to}`
    : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center justify-between gap-2">
          <span>Monthly spend</span>
          {drillHref && (
            <Link
              href={drillHref}
              className="text-xs font-normal text-muted-foreground hover:text-foreground hover:underline"
            >
              Open transactions
              {drillMonth ? ` · ${formatMonth(drillMonth)}` : ""}
            </Link>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={height}>
          <BarChart data={rows} margin={{ top: 5, right: 5, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
            <XAxis dataKey="month" fontSize={11} />
            <YAxis fontSize={11} tickFormatter={(v: number) => formatNIS(v)} />
            <Tooltip
              formatter={((value: number, name: string) => [formatNIS(value), name]) as unknown as never}
              cursor={{ fill: "var(--secondary)", opacity: 0.4 }}
            />
            {ccyOrder.length > 1 && <Legend wrapperStyle={{ fontSize: 12 }} />}
            {(fxMode === "nis" ? ["NIS"] : ccyOrder).map((c) => (
              <Bar
                key={c}
                dataKey={c}
                stackId="ccy"
                fill={CURRENCY_COLORS[c] ?? "hsl(0, 0%, 60%)"}
                isAnimationActive={false}
                onClick={handleBarClick}
                cursor="pointer"
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
