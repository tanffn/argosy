"use client";

import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api } from "@/lib/api";
import type { InsuranceGapsResponse } from "@/lib/retirement-types";

interface Props {
  monthlyIncomeNis: number;
  monthlyExpensesNis: number;
  dependentsCount: number;
  hasKidsUnder18: boolean;
  assetsNis: number;
  actualLifeCoverageNis?: number;
  actualDisabilityMonthlyNis?: number;
  actualLtcMonthlyNis?: number;
  actualHealthSupplementary?: boolean;
}

const TYPE_LABEL: Record<string, string> = {
  life: "Life insurance",
  disability: "Disability income",
  ltc: "Long-term care",
  health_supplementary: "Bituach Mashlim",
};

export function InsuranceGapsCard(props: Props) {
  const [data, setData] = useState<InsuranceGapsResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (api.retirement.insuranceGaps(props) as Promise<InsuranceGapsResponse>)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [props]);

  if (err) return <Card><CardHeader><CardTitle className="text-base">Insurance gaps</CardTitle><CardDescription className="text-rose-400">{err}</CardDescription></CardHeader></Card>;
  if (!data) return <Card><CardHeader><CardTitle className="text-base">Insurance gaps</CardTitle><CardDescription>Loading…</CardDescription></CardHeader></Card>;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Insurance gaps</CardTitle>
        <CardDescription>
          Recommended vs. actual coverage across 4 insurance types.
          Concrete action per gap.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2">
          {data.gaps.map((g) => {
            const gapVal = typeof g.gap.value === "number" ? g.gap.value : 0;
            const hasGap = gapVal > 0;
            return (
              <li key={g.insurance_type} className={`rounded-md border px-3 py-2 ${hasGap ? "border-amber-500/40 bg-amber-500/5" : "border-emerald-500/40 bg-emerald-500/5"}`}>
                <div className="flex items-baseline justify-between gap-2 flex-wrap">
                  <span className="text-sm font-medium">{TYPE_LABEL[g.insurance_type] ?? g.insurance_type}</span>
                  <span className={`text-xs font-mono ${hasGap ? "text-amber-400" : "text-emerald-400"}`}>
                    {hasGap ? "GAP" : "ADEQUATE"}
                  </span>
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  recommended <ValueWithTooltip data={g.recommended_coverage} />{" "}· actual <ValueWithTooltip data={g.actual_coverage} />{" "}· gap <ValueWithTooltip data={g.gap} />
                </div>
                <div className="mt-1 text-sm">{String(g.suggested_action.value ?? "")}</div>
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}
