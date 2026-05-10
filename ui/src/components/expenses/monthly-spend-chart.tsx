"use client";

import { useRouter } from "next/navigation";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type {
  ChartWindowBar,
  MonthlyTotalEntry,
} from "@/lib/expenses/api";

type SmallProps = {
  mode: "small";
  data: MonthlyTotalEntry[];
};
type FocalProps = {
  mode: "focal";
  chartWindow: ChartWindowBar[];
  onMonthSelected: (month: string) => void;
};
type Props = SmallProps | FocalProps;

function totalNisOf(entry: MonthlyTotalEntry): number {
  return entry.totals_by_currency?.NIS ?? 0;
}

export function MonthlySpendChart(props: Props) {
  const router = useRouter();

  if (props.mode === "small") {
    const series = props.data.map((e) => ({
      month: e.month.slice(2),
      key: e.month,
      total: Math.round(totalNisOf(e)),
    }));
    const handleClick = (payload: unknown) => {
      if (!payload || typeof payload !== "object") return;
      const p = payload as { key?: string };
      if (p.key) router.push(`/expenses/monthly?month=${p.key}`);
    };
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Monthly spend{" "}
            <span className="text-muted-foreground text-sm font-normal">click a bar to drill in</span>
          </CardTitle>
        </CardHeader>
        <CardContent className="h-32">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={series}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="month" />
              <YAxis hide />
              <Tooltip />
              <Bar
                dataKey="total"
                fill="#3b82f6"
                cursor="pointer"
                onClick={handleClick}
                isAnimationActive={false}
              />
            </BarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>
    );
  }

  // focal
  const series = props.chartWindow.map((b) => ({
    month: b.month.slice(2),
    key: b.month,
    total: Math.round(b.total_nis + b.total_usd * 3.7), // crude USD→NIS approximation
    is_padding: b.is_padding,
    is_selected: b.is_selected,
  }));
  const onMonthSelected = props.onMonthSelected;
  const handleClick = (payload: unknown) => {
    if (!payload || typeof payload !== "object") return;
    const p = payload as { key?: string; is_padding?: boolean };
    if (p.key && !p.is_padding) onMonthSelected(p.key);
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Monthly spend <span className="text-muted-foreground text-sm font-normal">±6 months</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={series}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="month" />
            <YAxis />
            <Tooltip />
            <Bar
              dataKey="total"
              cursor="pointer"
              onClick={handleClick}
              isAnimationActive={false}
            >
              {series.map((d, i) => (
                <Cell
                  key={i}
                  fill={
                    d.is_padding ? "#cbd5e1" :
                    d.is_selected ? "#1d4ed8" :
                    "#3b82f6"
                  }
                  fillOpacity={d.is_padding ? 0.3 : 1}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
