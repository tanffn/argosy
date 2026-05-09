"use client";

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
}

const CURRENCY_COLORS: Record<string, string> = {
  NIS: "hsl(220, 70%, 55%)",
  USD: "hsl(160, 65%, 50%)",
  EUR: "hsl(280, 65%, 60%)",
  GBP: "hsl(30, 80%, 55%)",
};

export function MonthlySpendChart({ data, fxMode, height = 280 }: MonthlySpendChartProps) {
  // Build chart data: one row per month, columns per currency.
  const currencies = new Set<string>();
  for (const m of data) for (const c of Object.keys(m.totals_by_currency)) currencies.add(c);
  const ccyOrder = ["NIS", "USD", "EUR", "GBP"].filter((c) => currencies.has(c));
  for (const c of currencies) if (!ccyOrder.includes(c)) ccyOrder.push(c);

  const rows = data.map((m) => {
    const row: Record<string, number | string> = { month: formatMonth(m.month) };
    for (const c of ccyOrder) row[c] = m.totals_by_currency[c] ?? 0;
    return row;
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Monthly spend</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={height}>
          <BarChart data={rows} margin={{ top: 5, right: 5, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
            <XAxis dataKey="month" fontSize={11} />
            <YAxis fontSize={11} tickFormatter={(v: number) => formatNIS(v)} />
            <Tooltip
              formatter={(value: number, name: string) => [formatNIS(value), name]}
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
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
