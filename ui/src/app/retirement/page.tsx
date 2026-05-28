"use client";

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { BituachLeumiCard } from "@/components/retirement/BituachLeumiCard";
import { DecumulationOrderCard } from "@/components/retirement/DecumulationOrderCard";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { GlidePathCard } from "@/components/retirement/GlidePathCard";
import { HealthcareCurveCard } from "@/components/retirement/HealthcareCurveCard";
import { HishtalmutTimerCard } from "@/components/retirement/HishtalmutTimerCard";
import { InsuranceGapsCard } from "@/components/retirement/InsuranceGapsCard";
import { LumpVsAnnuityCard } from "@/components/retirement/LumpVsAnnuityCard";
import { MekademBand } from "@/components/retirement/MekademBand";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { PhaseExpenseCard } from "@/components/retirement/PhaseExpenseCard";
import { RealEstateMortgageCard } from "@/components/retirement/RealEstateMortgageCard";
import { RebalancingAlertsCard } from "@/components/retirement/RebalancingAlertsCard";
import { RuinProbabilityHero } from "@/components/retirement/RuinProbabilityHero";
import { SafetyGatesPanel } from "@/components/retirement/SafetyGatesPanel";
import { SigmaCalibrationCard } from "@/components/retirement/SigmaCalibrationCard";
import { SourcesPanel } from "@/components/retirement/SourcesPanel";
import { StochasticFxCard } from "@/components/retirement/StochasticFxCard";
import { TaxBreakdownCard } from "@/components/retirement/TaxBreakdownCard";
import { WithdrawalPolicySelector } from "@/components/retirement/WithdrawalPolicySelector";

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
      <RuinProbabilityHero userId={USER_ID} retirementAge={49} targetPSolvent={0.9} />

      <SafetyGatesPanel userId={USER_ID} />

      <SigmaCalibrationCard userId={USER_ID} />

      <WithdrawalPolicySelector initialPolicyId="guyton_klinger" />

      <StochasticFxCard initialFx={3.4} horizonMonths={360} />

      <GlidePathCard currentAge={43} policy="vanguard_target_date" />

      <RebalancingAlertsCard userId={USER_ID} currentAge={43} />

      <PhaseExpenseCard hasKids={true} />

      <HealthcareCurveCard monthlyBurnNis={23000} />

      <TaxBreakdownCard userId={USER_ID} />

      <HishtalmutTimerCard
        userId={USER_ID}
        firstDepositDate="2018-01-01"
        currentAge={43}
      />

      <DecumulationOrderCard
        monthlyNeedNis={25000}
        taxableBalanceNis={3_000_000}
        hishtalmutBalanceNis={500_000}
        kupatGemelBalanceNis={75_000}
      />

      <LumpVsAnnuityCard
        pensionBalanceNis={1_500_000}
        mekademTypical={200}
        monthlyExpenseNeedNis={25_000}
      />

      <RealEstateMortgageCard
        primaryResidenceValueNis={3_500_000}
        mortgageBalanceNis={1_200_000}
        annualRate={0.045}
        termMonths={240}
      />

      <InsuranceGapsCard
        monthlyIncomeNis={55_000}
        monthlyExpensesNis={23_000}
        dependentsCount={2}
        hasKidsUnder18={true}
        assetsNis={3_500_000}
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
