"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import {
  SensitivityPanel,
  type SensitivityLever,
} from "@/components/retirement/SensitivityPanel";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { api, type BLStipendResponse } from "@/lib/api";

type BLResponse = BLStipendResponse;

interface Props {
  userId: string;
  currentAge: number;
  /** Years insured under BL. Defaults to current age - 22 (rough estimate). */
  contributionHistoryYears?: number;
  spouseEligible?: boolean;
}

/**
 * BL stipend hero card — shows the central estimate + band + key levers
 * the user can pull to improve their projection.
 *
 * Visualization: hero number on the left (₪X/mo · ON_TRACK badge if
 * history factor ≥ 0.9), band (low-high) on the right, top-3 sensitivity
 * levers below.
 */
export function BituachLeumiCard({
  userId,
  currentAge,
  contributionHistoryYears,
  spouseEligible = false,
}: Props) {
  const historyYears =
    contributionHistoryYears ?? Math.max(0, currentAge - 22);
  const [data, setData] = useState<BLResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .bituachLeumi(userId, currentAge, historyYears, spouseEligible)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [userId, currentAge, historyYears, spouseEligible]);

  if (err) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Bituach Leumi stipend</CardTitle>
          <CardDescription className="text-rose-400">Failed: {err}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Bituach Leumi stipend</CardTitle>
          <CardDescription className="flex items-center gap-2">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            Loading…
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const eligibilityAgeNum = typeof data.eligibility_age.value === "number"
    ? data.eligibility_age.value : 67;
  const historyFactor = typeof data.contribution_history_factor.value === "number"
    ? data.contribution_history_factor.value : 0;
  const fullEligible = historyFactor >= 0.9;

  const levers: SensitivityLever[] = data.sensitivity_levers
    .filter((l) => l.delta_nis_per_mo > 0)
    .map((l) => ({
      name: l.name,
      delta_pp: l.delta_nis_per_mo,
      direction: "up" as const,
      source: {
        value: l.delta_nis_per_mo,
        unit: "NIS/mo",
        source_id: l.source_id,
        rationale: l.name,
      },
    }));

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Bituach Leumi stipend · at age{" "}
          <ValueWithTooltip data={data.eligibility_age}>
            {eligibilityAgeNum}
          </ValueWithTooltip>
        </CardTitle>
        <CardDescription>
          Israeli social-security old-age stipend — a guaranteed-by-statute
          inflation-linked income floor in retirement. Worth ₪500K-1M+ NPV
          and was missing from prior projections.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Central estimate
            </div>
            <div className="mt-1 text-2xl font-mono font-semibold">
              <ValueWithTooltip data={data.monthly_nis} />
            </div>
            <div className="text-xs text-muted-foreground">
              {fullEligible ? (
                <span className="text-emerald-400">●FULL ELIGIBLE</span>
              ) : (
                <span className="text-amber-400">
                  ●HISTORY {Math.round(historyFactor * 100)}%
                </span>
              )}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Pessimistic band
            </div>
            <div className="mt-1 text-lg font-mono">
              <ValueWithTooltip data={data.monthly_nis_low} />
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Optimistic band
            </div>
            <div className="mt-1 text-lg font-mono">
              <ValueWithTooltip data={data.monthly_nis_high} />
            </div>
          </div>
        </div>

        <DrilldownSection title="Sensitivity — top levers" defaultOpen={false}>
          <SensitivityPanel levers={levers} unit="NIS/mo" />
        </DrilldownSection>

        <DrilldownSection title="Methodology">
          <MethodologyPanel>
            <p>
              The stipend = base rate × history-factor × (1 + spouse-supplement-if-eligible).
            </p>
            <ul className="list-disc pl-5">
              <li>
                Base rate: published Bituach Leumi old-age rate for the
                eligibility year, single-person.
              </li>
              <li>
                History factor: linear scale from 0.50 (no insured years)
                to 1.00 (35+ insured years). User history:{" "}
                <span className="font-mono">{historyYears}y</span>.
              </li>
              <li>
                Spouse supplement: ~50% of base when spouse is eligible
                (separate intake field).
              </li>
              <li>
                Bands shade the central estimate by −10% (low) and +5%
                (high) to account for rate updates + eligibility edges.
              </li>
            </ul>
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}
