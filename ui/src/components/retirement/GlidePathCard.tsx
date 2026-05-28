"use client";

import { useEffect, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { apiUrl } from "@/lib/api-base";

interface Props {
  currentAge?: number;
  policy?: "vanguard_target_date" | "age_minus_30_bonds";
}

interface ChartPoint {
  age: number;
  equity: number;
  bonds: number;
  cash: number;
}

/**
 * GlidePathCard — stacked-area chart of target allocation by age.
 *
 * Vertical reference line at current_age shows where the user is on the
 * glide-path today.
 */
export function GlidePathCard({ currentAge = 43, policy = "vanguard_target_date" }: Props) {
  const [rows, setRows] = useState<ChartPoint[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(apiUrl(`/api/retirement/glide-path?policy=${policy}&start_age=30&end_age=95`), { cache: "no-store" })
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((d) => {
        if (cancelled) return;
        const pts: ChartPoint[] = d.points.map((p: {age: number; target_equity_pct: {value: number}; target_bond_pct: {value: number}; target_cash_pct: {value: number}}) => ({
          age: p.age,
          equity: (p.target_equity_pct.value ?? 0) * 100,
          bonds: (p.target_bond_pct.value ?? 0) * 100,
          cash: (p.target_cash_pct.value ?? 0) * 100,
        }));
        setRows(pts);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [policy]);

  if (err) {
    return (
      <Card>
        <CardHeader><CardTitle className="text-base">Glide path</CardTitle><CardDescription className="text-rose-400">Failed: {err}</CardDescription></CardHeader>
      </Card>
    );
  }
  if (!rows) {
    return (
      <Card>
        <CardHeader><CardTitle className="text-base">Glide path</CardTitle><CardDescription>Loading…</CardDescription></CardHeader>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Glide path · {policy.replace(/_/g, " ")}</CardTitle>
        <CardDescription>
          Target equity / bond / cash allocation by age. Vertical line marks
          where you are today (age {currentAge}).
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={260}>
          <ComposedChart data={rows} margin={{ top: 8, right: 16, bottom: 4, left: 0 }} stackOffset="expand">
            <CartesianGrid strokeDasharray="3 3" opacity={0.18} />
            <XAxis dataKey="age" fontSize={11} ticks={[30, 40, 50, 60, 65, 75, 85, 95]} />
            <YAxis fontSize={10} tickFormatter={(v) => `${(v * 100).toFixed(0)}%`} domain={[0, 1]} />
            <Tooltip
              formatter={(value, name) => [`${(Number(value)).toFixed(1)}%`, String(name)]}
              labelFormatter={(label) => `age ${label}`}
            />
            <Area type="monotone" dataKey="equity" stackId="1" stroke="none" fill="#6366f1" fillOpacity={0.6} name="Equity" />
            <Area type="monotone" dataKey="bonds" stackId="1" stroke="none" fill="#14b8a6" fillOpacity={0.6} name="Bonds" />
            <Area type="monotone" dataKey="cash" stackId="1" stroke="none" fill="#a78bfa" fillOpacity={0.6} name="Cash" />
            <ReferenceLine x={currentAge} stroke="#facc15" strokeWidth={2} strokeDasharray="4 2" label={{ value: "today", position: "top", fill: "#facc15", fontSize: 10 }} />
          </ComposedChart>
        </ResponsiveContainer>

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              Vanguard target-date glide: 90% equity at 30, gradual ramp to
              50% at 65, then 40% equity post-75. Tracks empirically the
              risk capacity decline as wage-earning years shrink.
            </p>
            <p>
              When actual allocation drifts &gt; 5pp from the target band,
              the Rebalancing Alerts card surfaces a concrete buy/sell
              proposal to bring you back on path.
            </p>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
