"use client";

import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api } from "@/lib/api";
import type { DecumulationResponse } from "@/lib/retirement-types";

interface Props {
  monthlyNeedNis: number;
  taxableBalanceNis: number;
  hishtalmutBalanceNis: number;
  kupatGemelBalanceNis?: number;
  pensiaAnnuityMonthlyNis?: number;
}

const ACCOUNT_LABEL: Record<string, string> = {
  kupat_pensia_annuity: "Pension annuity",
  taxable: "Taxable accounts",
  kupat_gemel: "Kupat gemel",
  keren_hishtalmut: "Keren hishtalmut",
};

export function DecumulationOrderCard(props: Props) {
  const [data, setData] = useState<DecumulationResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (api.retirement.decumulationOrder({
      monthlyNeedNis: props.monthlyNeedNis,
      taxableBalanceNis: props.taxableBalanceNis,
      hishtalmutBalanceNis: props.hishtalmutBalanceNis,
      kupatGemelBalanceNis: props.kupatGemelBalanceNis ?? 0,
      pensiaAnnuityMonthlyNis: props.pensiaAnnuityMonthlyNis ?? 0,
    }) as Promise<DecumulationResponse>)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [props.monthlyNeedNis, props.taxableBalanceNis, props.hishtalmutBalanceNis, props.kupatGemelBalanceNis, props.pensiaAnnuityMonthlyNis]);

  if (err) return <Card><CardHeader><CardTitle className="text-base">Decumulation order</CardTitle><CardDescription className="text-rose-400">{err}</CardDescription></CardHeader></Card>;
  if (!data) return <Card><CardHeader><CardTitle className="text-base">Decumulation order</CardTitle><CardDescription>Loading…</CardDescription></CardHeader></Card>;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Decumulation order</CardTitle>
        <CardDescription>
          Which account to draw from first. Wrong order costs 10-15% of
          lifetime portfolio value via inefficient tax sequencing.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ol className="space-y-2">
          {data.steps.map((s) => (
            <li key={s.order} className="rounded-md border border-border/40 px-3 py-2 flex items-start gap-3">
              <span className="text-xs font-mono text-muted-foreground mt-0.5 min-w-[24px]">#{s.order}</span>
              <div className="flex-1">
                <div className="flex items-baseline justify-between flex-wrap gap-2">
                  <span className="text-sm font-medium">{ACCOUNT_LABEL[s.account] ?? s.account}</span>
                  <span className="font-mono text-sm">
                    <ValueWithTooltip data={s.monthly_draw_nis} />
                  </span>
                </div>
                <p className="mt-1 text-xs text-muted-foreground">{s.rationale}</p>
              </div>
            </li>
          ))}
        </ol>

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              Israeli tax efficiency for retirement decumulation:
            </p>
            <ol className="list-decimal pl-5">
              <li>Pension annuity first (already-converted; auto-flows)</li>
              <li>Taxable accounts (only gains taxed at 25%; cost basis free)</li>
              <li>Kupat gemel (per-vehicle rules; pre-2008 vs post-2008)</li>
              <li>Hishtalmut last (tax-free path; maximize compounding)</li>
            </ol>
            <p>
              In low-income years (e.g. waiting for annuity at 67), bracket-
              arbitrage opportunities arise — realize taxable gains at the
              lower marginal bracket.
            </p>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
