"use client";

import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { CategoryDeviation } from "@/lib/expenses/api";

interface Props {
  data: CategoryDeviation[];
  month: string | null;
}

function fmt(n: number) {
  return `₪${Math.round(n).toLocaleString("en-IL")}`;
}

export function CategoriesVsTypicalCard({ data, month }: Props) {
  if (!data || data.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Categories vs typical</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Not enough trailing-12 history yet.
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Categories vs typical{" "}
          <span className="text-muted-foreground text-sm font-normal">vs your trailing-12 baseline</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {data.map((d) => {
          const over = d.this_month_nis > d.typical_mean_nis;
          return (
            <Link
              key={d.slug}
              href={`/expenses/transactions?category=${encodeURIComponent(d.slug)}${month ? `&month=${month}` : ""}`}
              className="flex flex-col gap-0.5 hover:bg-secondary/40 px-2 -mx-2 py-1.5 rounded-sm"
            >
              <div className="flex items-center justify-between text-sm">
                <span>{d.label}</span>
                <span className={`font-mono tabular-nums ${over ? "text-error" : "text-success"}`}>
                  {fmt(d.this_month_nis)}
                  {d.delta_pct !== null && (
                    <span className="text-xs ml-1.5">
                      ({over ? "+" : ""}{Math.round(d.delta_pct * 100)}%)
                    </span>
                  )}
                </span>
              </div>
              <div className="text-xs text-muted-foreground">
                Usually {fmt(d.typical_mean_nis)} — z = {d.z_score.toFixed(1)}
              </div>
            </Link>
          );
        })}
      </CardContent>
    </Card>
  );
}
