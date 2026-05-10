"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { SavingsRatePoint } from "@/lib/expenses/api";

interface Props {
  data: SavingsRatePoint[];
}

export function SavingsRateTrend({ data }: Props) {
  if (data.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Savings rate</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Not enough data yet.
        </CardContent>
      </Card>
    );
  }
  const series = data.map((p) => ({
    month: p.month,
    rate: Math.round(p.savings_rate * 1000) / 10, // percent
  }));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Savings rate <span className="text-muted-foreground text-sm font-normal">(income − spending) / income</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={series}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="month" tickFormatter={(m: string) => m.slice(2)} />
            <YAxis tickFormatter={(v: number) => `${v}%`} />
            <Tooltip
              formatter={
                ((v: number) => [`${v}%`, "Savings rate"]) as unknown as never
              }
            />
            <Area type="monotone" dataKey="rate" stroke="#16a34a" fill="#16a34a33" />
          </AreaChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
