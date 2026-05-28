"use client";

import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api } from "@/lib/api";
import type { LumpVsAnnuityResponse } from "@/lib/retirement-types";

interface Props {
  pensionBalanceNis: number;
  mekademTypical?: number;
  monthlyExpenseNeedNis?: number;
  yearsRemaining?: number;
}

const REC_STYLE: Record<LumpVsAnnuityResponse["recommendation"], { dot: string; label: string }> = {
  take_annuity: { dot: "bg-emerald-500", label: "TAKE ANNUITY" },
  take_lump:    { dot: "bg-sky-500",     label: "TAKE LUMP" },
  split:        { dot: "bg-amber-500",   label: "SPLIT 50/50" },
};

export function LumpVsAnnuityCard(props: Props) {
  const [data, setData] = useState<LumpVsAnnuityResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (api.retirement.lumpVsAnnuity({
      pensionBalanceNis: props.pensionBalanceNis,
      mekademTypical: props.mekademTypical,
      monthlyExpenseNeedNis: props.monthlyExpenseNeedNis,
      yearsRemaining: props.yearsRemaining,
    }) as Promise<LumpVsAnnuityResponse>)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [props.pensionBalanceNis, props.mekademTypical, props.monthlyExpenseNeedNis, props.yearsRemaining]);

  if (err) return <Card><CardHeader><CardTitle className="text-base">Lump vs annuity</CardTitle><CardDescription className="text-rose-400">{err}</CardDescription></CardHeader></Card>;
  if (!data) return <Card><CardHeader><CardTitle className="text-base">Lump vs annuity</CardTitle><CardDescription>Loading…</CardDescription></CardHeader></Card>;

  const fmt = (n: number) => `₪${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  const style = REC_STYLE[data.recommendation];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          Lump vs annuity at age 67
          <span className={`inline-block h-2 w-2 rounded-full ${style.dot}`} aria-hidden />
          <span className="text-xs font-mono">{style.label}</span>
        </CardTitle>
        <CardDescription>
          {String(data.rationale.value ?? "")}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-3 gap-3">
          <div className={`rounded-md border px-3 py-3 ${data.recommendation === "take_annuity" ? "border-emerald-500/40 bg-emerald-500/5" : "border-border/40"}`}>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Full annuity</div>
            <div className="mt-1 text-lg font-mono font-semibold">{fmt(data.annuity_path.monthly_annuity_nis)}/mo</div>
            <div className="text-[10px] text-muted-foreground">NPV {fmt(data.annuity_path.lifetime_npv_nis)}</div>
          </div>
          <div className={`rounded-md border px-3 py-3 ${data.recommendation === "take_lump" ? "border-sky-500/40 bg-sky-500/5" : "border-border/40"}`}>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Full lump</div>
            <div className="mt-1 text-lg font-mono font-semibold">{fmt(data.lump_path.initial_lump_nis)}</div>
            <div className="text-[10px] text-muted-foreground">NPV {fmt(data.lump_path.lifetime_npv_nis)}</div>
            <div className="text-[10px] text-muted-foreground">end balance {fmt(data.lump_path.balance_at_end_nis)}</div>
          </div>
          <div className={`rounded-md border px-3 py-3 ${data.recommendation === "split" ? "border-amber-500/40 bg-amber-500/5" : "border-border/40"}`}>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">50/50 split</div>
            <div className="mt-1 text-lg font-mono font-semibold">{fmt(data.split_path.annuity_monthly_nis)}/mo</div>
            <div className="text-[10px] text-muted-foreground">NPV {fmt(data.split_path.lifetime_npv_nis)}</div>
          </div>
        </div>

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              At age 67, kupat_pensia + executive_insurance can convert
              to a lifelong annuity via mekadem (annuity = balance /
              mekadem), or in some funds be drawn as a partial lump.
            </p>
            <p>
              Recommendation heuristic:
            </p>
            <ul className="list-disc pl-5">
              <li>If annuity covers expenses → <b>take annuity</b> (safest; no sequence risk on essentials)</li>
              <li>If 4%-rule lump &gt; annuity × 1.5 → <b>take lump</b> (worth the upside)</li>
              <li>Otherwise → <b>split 50/50</b> (annuity floor + lump upside)</li>
            </ul>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
