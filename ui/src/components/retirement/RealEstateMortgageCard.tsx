"use client";

import { useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api } from "@/lib/api";
import type { MortgageScheduleResponse, RealEstateResponse } from "@/lib/retirement-types";

interface Props {
  primaryResidenceValueNis: number;
  mortgageBalanceNis: number;
  annualRate?: number;
  termMonths?: number;
  monthlyPropertyTaxNis?: number;
}

export function RealEstateMortgageCard(props: Props) {
  const [re, setRe] = useState<RealEstateResponse | null>(null);
  const [schedule, setSchedule] = useState<MortgageScheduleResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.retirement.realEstate({
        primaryResidenceValueNis: props.primaryResidenceValueNis,
        mortgageBalanceNis: props.mortgageBalanceNis,
        monthlyPropertyTaxNis: props.monthlyPropertyTaxNis,
      }) as Promise<RealEstateResponse>,
      props.mortgageBalanceNis > 0 && props.termMonths && props.annualRate != null
        ? api.retirement.mortgageSchedule({
            initialBalanceNis: props.mortgageBalanceNis,
            annualRate: props.annualRate,
            termMonths: props.termMonths,
          }) as Promise<MortgageScheduleResponse>
        : Promise.resolve(null),
    ])
      .then(([r, s]) => { if (!cancelled) { setRe(r); setSchedule(s); } })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [props.primaryResidenceValueNis, props.mortgageBalanceNis, props.annualRate, props.termMonths, props.monthlyPropertyTaxNis]);

  if (err) return <Card><CardHeader><CardTitle className="text-base">Real estate + mortgage</CardTitle><CardDescription className="text-rose-400">{err}</CardDescription></CardHeader></Card>;
  if (!re) return <Card><CardHeader><CardTitle className="text-base">Real estate + mortgage</CardTitle><CardDescription>Loading…</CardDescription></CardHeader></Card>;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Real estate + mortgage</CardTitle>
        <CardDescription>
          Primary residence equity (value − mortgage balance) + amortization schedule.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-3 gap-3 mb-4">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Value</div>
            <div className="mt-1 text-lg font-mono font-semibold"><ValueWithTooltip data={re.primary_residence_value_nis} /></div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Mortgage</div>
            <div className="mt-1 text-lg font-mono font-semibold"><ValueWithTooltip data={re.mortgage_balance_nis} /></div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Equity</div>
            <div className="mt-1 text-lg font-mono font-semibold text-emerald-400"><ValueWithTooltip data={re.equity_nis} /></div>
          </div>
        </div>

        {(!schedule || schedule.rows.length === 0) && props.mortgageBalanceNis > 0 && (
          <div className="text-xs text-muted-foreground mb-4">
            Amortization schedule needs the mortgage rate + term — add them on the
            intake page (no rate is assumed).
          </div>
        )}

        {schedule && schedule.rows.length > 0 && (
          <div>
            <div className="text-xs text-muted-foreground mb-2">
              Total interest over life of loan:{" "}
              <span className="font-mono">₪{schedule.total_interest_nis.toLocaleString()}</span>
              {" · "}
              Term: {(schedule.term_months / 12).toFixed(0)} years
            </div>
            <ResponsiveContainer width="100%" height={180}>
              <LineChart data={schedule.rows} margin={{ top: 4, right: 16, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" opacity={0.18} />
                <XAxis dataKey="month" tickFormatter={(m) => `${(m / 12).toFixed(0)}y`} fontSize={10} />
                <YAxis tickFormatter={(v) => `₪${(v / 1_000_000).toFixed(1)}M`} fontSize={10} />
                <Tooltip
                  formatter={((value: number | string) => {
                    const n = typeof value === "number" ? value : Number(value);
                    return `₪${n.toLocaleString()}`;
                  }) as unknown as never}
                  labelFormatter={(m) => `month ${m}`}
                />
                <Line type="monotone" dataKey="remaining_balance_nis" stroke="#6366f1" strokeWidth={2} dot={false} name="Remaining balance" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              Standard fixed-rate amortization. Israeli historical home
              appreciation ~3.5%/yr nominal (Bank of Israel 2000-2024
              median TLV metro). 10% illiquidity haircut applied to value
              when computing usable net worth (transaction costs + time-
              to-sell).
            </p>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
