"use client";

import { useEffect, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
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
import type { HealthcareCurveResponse } from "@/lib/retirement-types";

interface Props {
  monthlyBurnNis?: number;
}

export function HealthcareCurveCard({ monthlyBurnNis = 0 }: Props) {
  const [data, setData] = useState<HealthcareCurveResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (api.retirement.healthcareCurve({ startAge: 30, endAge: 95, monthlyBurnNis }) as Promise<HealthcareCurveResponse>)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [monthlyBurnNis]);

  if (err) return <Card><CardHeader><CardTitle className="text-base">Healthcare cost curve</CardTitle><CardDescription className="text-rose-400">{err}</CardDescription></CardHeader></Card>;
  if (!data) return <Card><CardHeader><CardTitle className="text-base">Healthcare cost curve</CardTitle><CardDescription>Loading…</CardDescription></CardHeader></Card>;

  const rows = data.curve.map((p) => ({
    age: p.age,
    cost: typeof p.monthly_cost_nis.value === "number" ? p.monthly_cost_nis.value : 0,
  }));

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Healthcare cost curve</CardTitle>
        <CardDescription>
          Israeli household healthcare expense by age: Mashlim + private + medications.
          Ramps materially post-65.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {data.share_of_burn_at_70 && (
          <div className="mb-3 text-sm">
            At age 70, healthcare is{" "}
            <span className="font-mono">
              <ValueWithTooltip data={data.share_of_burn_at_70} />
            </span>
            {" "}of household burn.
          </div>
        )}
        <ResponsiveContainer width="100%" height={180}>
          <AreaChart data={rows} margin={{ top: 4, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" opacity={0.18} />
            <XAxis dataKey="age" fontSize={10} />
            <YAxis tickFormatter={(v) => `₪${(v / 1000).toFixed(1)}K`} fontSize={10} />
            <Tooltip
              formatter={((value: number | string) => `₪${typeof value === "number" ? value.toLocaleString() : value}/mo`) as unknown as never}
              labelFormatter={(a) => `age ${a}`}
            />
            <Area type="step" dataKey="cost" stroke="#dc2626" fill="#dc2626" fillOpacity={0.25} />
          </AreaChart>
        </ResponsiveContainer>

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              Age-banded cost curve (nominal NIS/mo):
            </p>
            <ul className="list-disc pl-5 text-xs">
              <li>&lt; 55: ₪600/mo (Mashlim + dental)</li>
              <li>55-65: ₪900/mo (supplementary up + medication ramp)</li>
              <li>65-75: ₪1500/mo</li>
              <li>75-85: ₪2500/mo</li>
              <li>85+: ₪4000/mo (LTC creep)</li>
            </ul>
            <p>
              OECD Israel data shows ~1.5%/yr real growth above CPI for
              elderly cohorts; applied separately via phase_expenses
              inflation_premium.
            </p>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
