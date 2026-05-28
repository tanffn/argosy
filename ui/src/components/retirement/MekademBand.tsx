"use client";

import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api, type MekademBandResponse } from "@/lib/api";

interface Props {
  userId: string;
  /** Which Israeli pension fund the user holds. */
  fundId: "clal_pensia" | "migdal_pensia" | "menorah_pensia";
  /** Optional: pension balance to drive the corresponding annuity band. */
  balanceNis?: number;
}

const FUND_LABEL: Record<Props["fundId"], string> = {
  clal_pensia: "Clal kupat pensia",
  migdal_pensia: "Migdal kupat pensia",
  menorah_pensia: "Menorah kupat pensia",
};

/**
 * Mekadem variance band card. Displays (low / typical / high) mekadem and
 * — if balance supplied — the corresponding monthly-annuity band.
 *
 * Replaces the prior "fixed mekadem = 200" assumption from the cashflow
 * projection. The band reflects 2.5% uncertainty around the published
 * typical, covering spouse-benefit and mortality-table effects.
 */
export function MekademBand({ userId, fundId, balanceNis }: Props) {
  const [data, setData] = useState<MekademBandResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .mekadem(fundId, userId, balanceNis)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [userId, fundId, balanceNis]);

  if (err) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Mekadem · {FUND_LABEL[fundId]}</CardTitle>
          <CardDescription className="text-rose-400">Failed: {err}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Mekadem · {FUND_LABEL[fundId]}</CardTitle>
          <CardDescription>Loading…</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Mekadem · {FUND_LABEL[fundId]}
        </CardTitle>
        <CardDescription>
          Annuity coefficient at retirement age 67. Annuity = balance / mekadem,
          so a lower mekadem means a higher monthly annuity. The band
          (low / typical / high) shades the uncertainty in your actual policy.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Low (favorable)
            </div>
            <div className="mt-1 text-2xl font-mono font-semibold text-emerald-400">
              <ValueWithTooltip data={data.low} />
            </div>
            {data.annuity_monthly_nis_high && (
              <div className="text-xs text-muted-foreground">
                annuity ≈{" "}
                <ValueWithTooltip data={data.annuity_monthly_nis_high} />/mo
              </div>
            )}
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Typical
            </div>
            <div className="mt-1 text-2xl font-mono font-semibold">
              <ValueWithTooltip data={data.typical} />
            </div>
            {data.annuity_monthly_nis_typical && (
              <div className="text-xs text-muted-foreground">
                annuity ≈{" "}
                <ValueWithTooltip data={data.annuity_monthly_nis_typical} />/mo
              </div>
            )}
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              High (unfavorable)
            </div>
            <div className="mt-1 text-2xl font-mono font-semibold text-rose-400">
              <ValueWithTooltip data={data.high} />
            </div>
            {data.annuity_monthly_nis_low && (
              <div className="text-xs text-muted-foreground">
                annuity ≈{" "}
                <ValueWithTooltip data={data.annuity_monthly_nis_low} />/mo
              </div>
            )}
          </div>
        </div>

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              The mekadem (annuity coefficient) varies by fund + age + plan
              + spouse-benefit selection + mortality table. Argosy uses the
              published typical for each major Israeli pension fund and
              wraps a ±2.5% band around it covering the uncertainty in
              your exact policy details.
            </p>
            <ul className="list-disc pl-5">
              <li>
                Lower mekadem (favorable band) → higher monthly annuity.
              </li>
              <li>
                If you've supplied a pension balance, the corresponding
                annuity band is shown under each mekadem value.
              </li>
              <li>
                To replace the band with your exact policy value, add it
                to <code>identity_yaml.retirement_reference_overrides</code>{" "}
                under the key{" "}
                <code>mekadem.{fundId}</code>.
              </li>
            </ul>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
