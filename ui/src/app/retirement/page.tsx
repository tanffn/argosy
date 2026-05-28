"use client";

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { BituachLeumiCard } from "@/components/retirement/BituachLeumiCard";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { HeroCard } from "@/components/retirement/HeroCard";
import { MekademBand } from "@/components/retirement/MekademBand";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { SourcesPanel } from "@/components/retirement/SourcesPanel";

const USER_ID = "ariel";

/**
 * Retirement companion page — built incrementally across 7 waves.
 *
 * Plan: docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md
 *
 * Wave 0: hero scaffold + UI primitives + sources panel
 * Wave 1 (current): BL stipend card + mekadem variance band
 * Later waves: safety gates · P(ruin) gate · glide path · tax engine · ...
 */
export default function RetirementPage() {
  return (
    <div className="container mx-auto px-4 py-6 max-w-5xl space-y-4">
      <HeroCard
        title="Retirement readiness"
        status="UNCERTAIN"
        verdict="Wave 0 scaffold — full verdict ships in Wave 3 (probability-of-ruin gate)."
        numbers={[
          {
            label: "P(solvent at 95)",
            display: "—",
            secondary: "Wave 3",
          },
          {
            label: "Retire-ready age",
            display: "—",
            secondary: "Wave 3",
          },
          {
            label: "Safety gates",
            display: "—",
            secondary: "Wave 2",
          },
        ]}
      />

      <BituachLeumiCard
        userId={USER_ID}
        currentAge={43}
        contributionHistoryYears={21}
        spouseEligible={false}
      />

      <MekademBand
        userId={USER_ID}
        fundId="clal_pensia"
        balanceNis={1_500_000}
      />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Coming up</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          <ul className="list-disc pl-5 space-y-1">
            <li>
              <b>Wave 1:</b> Mekadem variance band + Bituach Leumi stipend module
            </li>
            <li>
              <b>Wave 2:</b> Safety gates (NRA estate · emergency liquidity)
            </li>
            <li>
              <b>Wave 3:</b> Probability-of-ruin gate + sigma auto-calibration +
              regime-switch MC + stochastic FX + withdrawal policy + conflict
              scenario gate
            </li>
            <li>
              <b>Wave 4:</b> Glide path + rebalancing + lifecycle income + phase
              expenses + IDF service + healthcare
            </li>
            <li>
              <b>Wave 5:</b> Account-aware tax engine + decumulation + lump-vs-
              annuity + hishtalmut + kupat-gemel
            </li>
            <li>
              <b>Wave 6:</b> Real estate · mortgage · partner · severance split
            </li>
            <li>
              <b>Wave 7:</b> Insurance gaps + action engine + replan triggers +
              multi-goal + behavioral + route dedup
            </li>
          </ul>
        </CardContent>
      </Card>

      <DrilldownSection title="Methodology" defaultOpen={false}>
        <MethodologyPanel>
          <p>
            The retirement companion follows a "hero + chart + drill-down"
            standard. Top: a verdict card with the one-line answer + 1-3 key
            numbers. Middle: the relevant chart (cashflow / Monte Carlo /
            glide path). Bottom: collapsible drill-down sections like this
            one for the methodology, sensitivity analysis, and sources.
          </p>
          <p>
            Every value on the page passes through the{" "}
            <code>ValueWithRationale</code> shape: the value plus its source
            plus its rationale plus any freshness warnings. Hover over any
            dotted-underline number to see the explanation.
          </p>
        </MethodologyPanel>
      </DrilldownSection>

      <DrilldownSection title="Sources" badge="all">
        <SourcesPanel filterIds={null} />
      </DrilldownSection>
    </div>
  );
}
