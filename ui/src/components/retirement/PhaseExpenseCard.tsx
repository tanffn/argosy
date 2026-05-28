"use client";

import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api } from "@/lib/api";
import type { PhaseExpensesResponse } from "@/lib/retirement-types";

interface Props {
  hasKids?: boolean;
}

export function PhaseExpenseCard({ hasKids = true }: Props) {
  const [data, setData] = useState<PhaseExpensesResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (api.retirement.phaseExpenses(hasKids) as Promise<PhaseExpensesResponse>)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [hasKids]);

  if (err) return <Card><CardHeader><CardTitle className="text-base">Expense phases</CardTitle><CardDescription className="text-rose-400">{err}</CardDescription></CardHeader></Card>;
  if (!data) return <Card><CardHeader><CardTitle className="text-base">Expense phases</CardTitle><CardDescription>Loading…</CardDescription></CardHeader></Card>;

  const COLORS: Record<string, string> = {
    kids_peak: "border-amber-500/40 bg-amber-500/5",
    empty_nest: "border-emerald-500/40 bg-emerald-500/5",
    healthcare_ramp: "border-rose-500/40 bg-rose-500/5",
    late_life_ltc: "border-rose-600/40 bg-rose-600/10",
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Expense phases over life</CardTitle>
        <CardDescription>
          Moves beyond flat × inflation. Kids peak (1.1×) → empty nest (0.85×) → healthcare ramp (1.1× + 1.5%/yr) → late-life LTC tail (1.15× + 3%/yr).
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2">
          {data.phases.map((p) => (
            <li key={p.label} className={`rounded-md border px-3 py-2 ${COLORS[p.label] ?? "border-border/40"}`}>
              <div className="flex items-baseline justify-between gap-2 flex-wrap">
                <span className="text-sm font-medium capitalize">{p.label.replace(/_/g, " ")}</span>
                <span className="text-xs font-mono text-muted-foreground">ages {p.start_age}-{p.end_age}</span>
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                multiplier <ValueWithTooltip data={p.monthly_multiplier} />{" "}
                · extra inflation <ValueWithTooltip data={p.inflation_premium} />/yr above CPI
              </div>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
