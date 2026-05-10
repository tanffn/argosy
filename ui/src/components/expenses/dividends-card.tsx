"use client";

import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis } from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type DividendsSummary } from "@/lib/expenses/api";

const USD_FMT = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", maximumFractionDigits: 0,
});
const USD_FMT_2DP = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2,
});

interface DividendsCardProps {
  data: DividendsSummary;
}

function MiniBars({ values, height = 28 }: { values: number[]; height?: number }) {
  const max = Math.max(1, ...values);
  return (
    <svg width={values.length * 6} height={height} className="block">
      {values.map((v, i) => {
        const h = Math.max(1, Math.round((v / max) * (height - 4)));
        return (
          <rect
            key={i}
            x={i * 6}
            y={height - h - 2}
            width={4}
            height={h}
            fill="currentColor"
            className="text-emerald-600"
          />
        );
      })}
    </svg>
  );
}

export function DividendsCard({ data }: DividendsCardProps) {
  const series = data.monthly_series ?? [];
  const hasSeries = series.length > 0;
  const trend = (data.trend_12mo ?? []).map((p) => p.total_usd);
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          Dividends
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex items-baseline justify-between gap-3">
          <div>
            <div className="text-2xl font-semibold text-emerald-600">
              {USD_FMT_2DP.format(data.current_month_total_usd ?? 0)}
            </div>
            <div className="text-xs text-muted-foreground">
              this month
              {data.month ? ` (${data.month})` : ""}
            </div>
          </div>
          <div className="text-right">
            <div className="text-base font-medium">
              {USD_FMT.format(data.yearly_total_usd ?? 0)}
            </div>
            <div className="text-xs text-muted-foreground">last 12mo</div>
          </div>
        </div>
        {trend.length > 0 && (
          <div className="mt-2">
            <MiniBars values={trend} />
          </div>
        )}
        {hasSeries && (
          <div className="mt-3 h-16">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={series}
                margin={{ top: 4, right: 4, left: 0, bottom: 0 }}
              >
                <XAxis dataKey="month" hide />
                <Tooltip
                  formatter={(v) => USD_FMT_2DP.format(Number(v))}
                  labelFormatter={(l) => String(l)}
                />
                <Line
                  type="monotone"
                  dataKey="total_usd"
                  stroke="hsl(150, 60%, 45%)"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
        {!hasSeries && (
          <div className="mt-3 text-xs text-muted-foreground">
            No dividend rows detected yet.
          </div>
        )}
      </CardContent>
    </Card>
  );
}
