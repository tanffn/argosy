"use client";

import { useEffect, useState } from "react";

import { api, type WithdrawalPolicy } from "@/lib/api";

import { BituachLeumiCard } from "@/components/retirement/BituachLeumiCard";
import { DecumulationOrderCard } from "@/components/retirement/DecumulationOrderCard";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { ExpectedRetirementAgeCard } from "@/components/retirement/ExpectedRetirementAgeCard";
import { GlidePathCard } from "@/components/retirement/GlidePathCard";
import { HealthcareCurveCard } from "@/components/retirement/HealthcareCurveCard";
import { HishtalmutTimerCard } from "@/components/retirement/HishtalmutTimerCard";
import { HolisticTimelineCard } from "@/components/retirement/HolisticTimelineCard";
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
import { UpcomingVestCard } from "@/components/retirement/UpcomingVestCard";
import { WithdrawalPolicySelector } from "@/components/retirement/WithdrawalPolicySelector";

const USER_ID = "ariel";

// Fix UX #6 — page-internal section anchors with a sticky TOC.
// Allocation actions (formerly "Windfall" section here) moved to
// /proposals#allocation in sprint commit #6 — /retirement is now a
// read-only visualization surface.
const SECTIONS: Array<{ id: string; label: string }> = [
  { id: "when-can-i-retire", label: "When can I retire?" },
  { id: "timeline",          label: "Holistic Timeline" },
  { id: "upcoming-vests",    label: "Upcoming RSU vests" },
  { id: "verdict",           label: "Verdict" },
  { id: "safety",        label: "Safety gates" },
  { id: "predictions",   label: "Prediction trust" },
  { id: "decision",      label: "Decision policy" },
  { id: "expenses",      label: "Expense modeling" },
  { id: "tax",           label: "Tax" },
  { id: "decumulation",  label: "Decumulation" },
  { id: "balance",       label: "Balance sheet" },
  { id: "risk-transfer", label: "Risk transfer" },
  { id: "israeli",       label: "Israeli structure" },
  { id: "sources",       label: "Sources" },
];

/**
 * Retirement companion page — built incrementally across 7 waves.
 *
 * Plan: docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md
 *
 * Fix #1 (sigma pipe): the page fetches sigma-calibration once on mount
 * and passes the calibrated σ to <RuinProbabilityHero> so the verdict
 * actually consumes the auto-calibrated NVDA-aware σ instead of the
 * 0.18 diversified default that compute_ruin_probability would otherwise
 * fall back to.
 */
export default function RetirementPage() {
  const [policy, setPolicy] = useState<WithdrawalPolicy["id"]>("guyton_klinger");
  const [calibratedSigma, setCalibratedSigma] = useState<number | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .sigmaCalibration(USER_ID)
      .then((d) => {
        if (cancelled) return;
        const v = d.sigma_annual.value;
        if (typeof v === "number") setCalibratedSigma(v);
      })
      .catch(() => {
        // Hero falls back to engine default if calibration fails.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="container mx-auto px-4 py-6 max-w-6xl grid grid-cols-1 lg:grid-cols-[200px_1fr] gap-6">
      {/* Fix UX #6 — sticky section TOC */}
      <nav className="hidden lg:block sticky top-6 self-start text-sm">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-2">
          Jump to
        </div>
        <ul className="space-y-1.5">
          {SECTIONS.map((s) => (
            <li key={s.id}>
              <a
                href={`#${s.id}`}
                className="text-muted-foreground hover:text-foreground transition-colors block border-l-2 border-transparent hover:border-foreground/40 pl-2 -ml-0.5"
              >
                {s.label}
              </a>
            </li>
          ))}
        </ul>
      </nav>

      <div className="space-y-4 min-w-0">
        {/* Expected retirement age headline (2026-05-29) — the most
            direct answer to "when can I retire?" surfaced as a card
            ABOVE the P(solvent) verdict. Sourced from the cashflow
            projection on the current plan draft. */}
        <section id="when-can-i-retire" className="scroll-mt-6">
          <ExpectedRetirementAgeCard userId={USER_ID} />
        </section>

        <section id="timeline" className="scroll-mt-6">
          <HolisticTimelineCard userId={USER_ID} />
        </section>

        {/* Sprint #2 commit #12 — three-scenario tax outlook +
            allocation preview for upcoming RSU vests. Between the
            holistic timeline ("what's the long-range schedule?") and
            the safety section. */}
        <section id="upcoming-vests" className="scroll-mt-6">
          <UpcomingVestCard userId={USER_ID} />
        </section>

        <section id="verdict" className="scroll-mt-6">
          <RuinProbabilityHero
            userId={USER_ID}
            retirementAge={49}
            targetPSolvent={0.9}
            withdrawalPolicyId={policy}
            sigmaAnnual={calibratedSigma}
          />
        </section>

        <section id="safety" className="scroll-mt-6">
          <SafetyGatesPanel userId={USER_ID} />
        </section>

        <section id="predictions" className="scroll-mt-6 space-y-4">
          <SigmaCalibrationCard userId={USER_ID} />
          <WithdrawalPolicySelector
            initialPolicyId="guyton_klinger"
            onChange={(id) => setPolicy(id)}
          />
          <StochasticFxCard initialFx={3.4} horizonMonths={360} />
        </section>

        <section id="decision" className="scroll-mt-6 space-y-4">
          <GlidePathCard currentAge={43} policy="vanguard_target_date" />
          <RebalancingAlertsCard userId={USER_ID} currentAge={43} />
        </section>

        <section id="expenses" className="scroll-mt-6 space-y-4">
          <PhaseExpenseCard hasKids={true} />
          <HealthcareCurveCard monthlyBurnNis={23000} />
        </section>

        <section id="tax" className="scroll-mt-6 space-y-4">
          <TaxBreakdownCard userId={USER_ID} />
          <HishtalmutTimerCard
            userId={USER_ID}
            firstDepositDate="2018-01-01"
            currentAge={43}
          />
        </section>

        <section id="decumulation" className="scroll-mt-6 space-y-4">
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
        </section>

        <section id="balance" className="scroll-mt-6">
          <RealEstateMortgageCard
            primaryResidenceValueNis={3_500_000}
            mortgageBalanceNis={1_200_000}
            annualRate={0.045}
            termMonths={240}
          />
        </section>

        <section id="risk-transfer" className="scroll-mt-6">
          <InsuranceGapsCard
            monthlyIncomeNis={55_000}
            monthlyExpensesNis={23_000}
            dependentsCount={2}
            hasKidsUnder18={true}
            assetsNis={3_500_000}
          />
        </section>

        <section id="israeli" className="scroll-mt-6 space-y-4">
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
        </section>

        <section id="sources" className="scroll-mt-6">
          <DrilldownSection title="Methodology" defaultOpen={false}>
            <MethodologyPanel>
              <p>
                The retirement companion follows a &quot;hero + chart +
                drill-down&quot; standard. Top: a verdict card with the
                one-line answer + 1-3 key numbers. Middle: the relevant
                chart. Bottom: collapsible drill-down sections like this
                one for the methodology, sensitivity analysis, and sources.
              </p>
              <p>
                Every value on the page passes through the{" "}
                <code>ValueWithRationale</code> shape: the value plus its
                source plus its rationale plus any freshness warnings.
                Hover any dotted-underline number to see the explanation.
              </p>
            </MethodologyPanel>
          </DrilldownSection>

          <DrilldownSection title="Sources" badge="all">
            <SourcesPanel filterIds={null} />
          </DrilldownSection>
        </section>
      </div>
    </div>
  );
}
