"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { CurrencyMixPoint } from "@/lib/expenses/api";

interface Props {
  data: CurrencyMixPoint[];
}

export function CurrencyMixCard({ data }: Props) {
  if (!data || data.length === 0) {
    return (
      <Card>
        <CardHeader><CardTitle className="text-base">Currency mix</CardTitle></CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          No data.
        </CardContent>
      </Card>
    );
  }
  const series = data.map((p) => ({
    month: p.month.slice(2),
    NIS: Math.round(p.nis),
    USD: Math.round(p.usd),
  }));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Currency mix <span className="text-muted-foreground text-sm font-normal">trailing 12 months</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={series}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="month" />
            <YAxis />
            <Tooltip />
            <Legend />
            <Bar dataKey="NIS" stackId="a" fill="#3b82f6" />
            <Bar dataKey="USD" stackId="a" fill="#a855f7" />
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
