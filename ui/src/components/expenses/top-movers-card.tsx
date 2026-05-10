"use client";

import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { TopMovers } from "@/lib/expenses/api";

interface Props {
  data: TopMovers;
}

function fmt(n: number) {
  return `₪${Math.round(n).toLocaleString("en-IL")}`;
}

function MoverRow({ slug, label, delta_nis, delta_pct }: {
  slug: string; label: string; delta_nis: number; delta_pct: number | null;
}) {
  const positive = delta_nis > 0;
  return (
    <Link
      href={`/expenses/transactions?category=${encodeURIComponent(slug)}`}
      className="flex items-center justify-between gap-2 py-1.5 hover:bg-secondary/40 rounded-sm px-2 -mx-2"
    >
      <span className="text-sm">{label}</span>
      <span className={`text-sm tabular-nums ${positive ? "text-emerald-600" : "text-rose-600"}`}>
        {positive ? "+" : ""}{fmt(delta_nis)}
        {delta_pct !== null && (
          <span className="text-xs text-muted-foreground ml-1.5">
            ({Math.round(delta_pct * 100)}%)
          </span>
        )}
      </span>
    </Link>
  );
}

export function TopMoversCard({ data }: Props) {
  if (data.reason === "insufficient_history") {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Top movers</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Need at least 12 months of data to compare.
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Top movers <span className="text-muted-foreground text-sm font-normal">last 6 months vs prior 6</span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <div className="text-xs uppercase text-muted-foreground mb-1">Grew</div>
            {data.grew.length === 0 ? (
              <div className="text-xs text-muted-foreground">None.</div>
            ) : data.grew.map((d) => <MoverRow key={d.slug} {...d} />)}
          </div>
          <div>
            <div className="text-xs uppercase text-muted-foreground mb-1">Shrank</div>
            {data.shrank.length === 0 ? (
              <div className="text-xs text-muted-foreground">None.</div>
            ) : data.shrank.map((d) => <MoverRow key={d.slug} {...d} />)}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
