"use client";

import Link from "next/link";
import {
  Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { type CategorySpend } from "@/lib/expenses/api";
import { colorForSlug, formatNIS, formatPercent } from "@/lib/expenses/format";

interface CategoryDonutProps {
  data: CategorySpend[];
  height?: number;
}

export function CategoryDonut({ data, height = 280 }: CategoryDonutProps) {
  const total = data.reduce((s, c) => s + c.total_nis, 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Categories — current month</CardTitle>
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <div className="text-sm text-muted-foreground py-12 text-center">
            No data this month yet.
          </div>
        ) : (
          <div className="flex flex-col lg:flex-row gap-4 items-center">
            <ResponsiveContainer width="100%" height={height}>
              <PieChart>
                <Pie
                  data={data}
                  dataKey="total_nis"
                  nameKey="label_en"
                  innerRadius={60}
                  outerRadius={100}
                  paddingAngle={2}
                  isAnimationActive={false}
                >
                  {data.map((c) => (
                    <Cell key={c.slug} fill={colorForSlug(c.slug)} />
                  ))}
                </Pie>
                <Tooltip
                  formatter={((value: number, _: string, item: { payload: CategorySpend }) => [
                    `${formatNIS(value)} (${formatPercent(item.payload.percent)})`,
                    item.payload.label_en,
                  ]) as unknown as never}
                />
              </PieChart>
            </ResponsiveContainer>
            <div className="flex-1 flex flex-col gap-1 text-sm">
              <div className="text-xs text-muted-foreground mb-1">
                Total: {formatNIS(total)}
              </div>
              {data.slice(0, 8).map((c) => (
                <Link
                  key={c.slug}
                  href={`/expenses/transactions?category=${encodeURIComponent(c.slug)}`}
                  className="flex items-center gap-2 hover:bg-secondary/40 px-2 py-1 rounded"
                >
                  <span
                    className="w-3 h-3 rounded-sm shrink-0"
                    style={{ background: colorForSlug(c.slug) }}
                  />
                  <span className="capitalize flex-1 truncate">{c.label_en}</span>
                  <span className="text-muted-foreground tabular-nums">
                    {formatNIS(c.total_nis)}
                  </span>
                  <span className="text-xs text-muted-foreground w-12 text-right tabular-nums">
                    {formatPercent(c.percent)}
                  </span>
                </Link>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
