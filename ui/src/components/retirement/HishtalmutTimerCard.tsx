"use client";

import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api } from "@/lib/api";
import type { HishtalmutEligibilityResponse } from "@/lib/retirement-types";

interface Props {
  userId: string;
  firstDepositDate: string;  // "YYYY-MM-DD"
  currentAge: number;
}

export function HishtalmutTimerCard({ userId, firstDepositDate, currentAge }: Props) {
  const [data, setData] = useState<HishtalmutEligibilityResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (api.retirement.hishtalmutEligibility(userId, firstDepositDate, currentAge) as Promise<HishtalmutEligibilityResponse>)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [userId, firstDepositDate, currentAge]);

  if (err) return <Card><CardHeader><CardTitle className="text-base">Hishtalmut eligibility</CardTitle><CardDescription className="text-rose-400">{err}</CardDescription></CardHeader></Card>;
  if (!data) return <Card><CardHeader><CardTitle className="text-base">Hishtalmut eligibility</CardTitle><CardDescription>Loading…</CardDescription></CardHeader></Card>;

  const taxfree = (data.taxfree_now.value as number) === 1;
  const months = data.months_until_taxfree.value as number;
  const years = Math.floor(months / 12);
  const remMonths = months % 12;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          Hishtalmut eligibility
          {taxfree ? (
            <span className="text-xs font-mono text-emerald-400">●TAX-FREE NOW</span>
          ) : (
            <span className="text-xs font-mono text-amber-400">●WAITING</span>
          )}
        </CardTitle>
        <CardDescription>
          §3(e) tax-free if 6yr from first deposit OR age 67. Both paths
          checked independently.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="text-3xl font-mono font-semibold">
          {taxfree ? "₪0 tax due" : `${years}y ${remMonths}mo to go`}
        </div>
        <div className="mt-2 grid grid-cols-2 gap-3 text-sm">
          <div>
            <span className="text-muted-foreground">6yr path:</span>{" "}
            <ValueWithTooltip data={data.six_yr_eligible}>
              {(data.six_yr_eligible.value as number) === 1 ? "✓ eligible" : "× waiting"}
            </ValueWithTooltip>
          </div>
          <div>
            <span className="text-muted-foreground">Age-67 path:</span>{" "}
            <ValueWithTooltip data={data.age_67_eligible}>
              {(data.age_67_eligible.value as number) === 1 ? "✓ eligible" : "× waiting"}
            </ValueWithTooltip>
          </div>
          <div>
            <span className="text-muted-foreground">First deposit:</span>{" "}
            <ValueWithTooltip data={data.first_deposit_date} />
          </div>
          <div>
            <span className="text-muted-foreground">If withdrawn now:</span>{" "}
            {taxfree ? "₪0 tax" : `× ${((data.early_withdrawal_marginal_rate.value as number) * 100).toFixed(0)}% marginal`}
          </div>
        </div>

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              Israeli Income Tax Ordinance §3(e) ships two distinct
              tax-free paths — only ONE must be satisfied:
            </p>
            <ul className="list-disc pl-5">
              <li>6 years from first deposit (employee-deposited)</li>
              <li>Age 67 lump path (any holding period)</li>
            </ul>
            <p>
              Early withdrawal that fails BOTH paths is taxed at the
              user's marginal income tax rate (~47% top bracket).
            </p>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
