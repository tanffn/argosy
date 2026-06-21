"use client";

import { useEffect, useState } from "react";

import { api, type DerivedInputsResponse, type WithdrawalPolicy } from "@/lib/api";

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
import { ScenarioGridCard } from "@/components/retirement/ScenarioGridCard";
import { DerivedInputsProvenancePanel } from "@/components/retirement/DerivedInputsProvenancePanel";
import { SigmaCalibrationCard } from "@/components/retirement/SigmaCalibrationCard";
import { SourcesPanel } from "@/components/retirement/SourcesPanel";
import { PlanStoryLead } from "@/components/overview/PlanStoryLead";
import { StochasticFxCard } from "@/components/retirement/StochasticFxCard";
import { TaxBreakdownCard } from "@/components/retirement/TaxBreakdownCard";
import { WithdrawalPolicySelector } from "@/components/retirement/WithdrawalPolicySelector";
import { CollapsibleSection } from "@/components/ui/collapsible-section";

const USER_ID = "ariel";

// Page-internal jump rail. The page LEADS with the plain-language plan
// story + the headline answer + the verdict (all visible by default), then
// groups the expert depth into collapsible, lazy-mounted sections so the
// page opens clean and the slow Monte-Carlo cards don't fetch until their
// section is expanded.
const SECTIONS: Array<{ id: string; label: string }> = [
  { id: "when-can-i-retire", label: "When can I retire?" },
  { id: "verdict", label: "Verdict" },
  { id: "safety", label: "Safety & withdrawal policy" },
  { id: "taxes", label: "Taxes" },
  { id: "expenses", label: "Expenses & life phases" },
  { id: "income", label: "Income, real estate & insurance" },
  { id: "methodology", label: "Methodology & sources" },
];

/**
 * Retirement companion page.
 *
 * Visible by default: PlanStoryLead (the plain-language spine),
 * ExpectedRetirementAgeCard (the headline answer), and the verdict
 * (ScenarioGridCard + RuinProbabilityHero). Everything else lives in
 * collapsible sections that mount their children only when expanded
 * (CollapsibleSection renders `children` only while `open`), so the
 * page de-clutters AND the expert cards' slow backend calls are deferred
 * until the user opens the section.
 *
 * Sigma pipe: the page fetches sigma-calibration once on mount and passes
 * the calibrated σ to <RuinProbabilityHero> so the verdict consumes the
 * auto-calibrated NVDA-aware σ instead of the 0.18 diversified default.
 */
export default function RetirementPage() {
  const [policy, setPolicy] = useState<WithdrawalPolicy["id"]>("guyton_klinger");
  const [calibratedSigma, setCalibratedSigma] = useState<number | undefined>(undefined);

  // Output-trust doctrine: every numeric/age/balance prop the cards consume
  // comes from this single backend payload — the page carries ZERO hardcoded
  // financial magic numbers. Fields are {value, unit, source, confidence,
  // status}; a "pending" field has value:null and must NOT be faked.
  const [derived, setDerived] = useState<DerivedInputsResponse | null>(null);

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

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .derivedInputs(USER_ID)
      .then((d) => {
        if (!cancelled) setDerived(d);
      })
      .catch(() => {
        // Leave `derived` null — derived-dependent cards stay in their
        // loading placeholder rather than rendering invented numbers.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Helpers that pull a value out of the derived payload. They return
  // `undefined` until the fetch resolves so callers can gate rendering;
  // a "pending" field (value:null) also reads as undefined → no fake number.
  const numOf = (key: keyof DerivedInputsResponse): number | undefined => {
    if (!derived) return undefined;
    const field = derived[key];
    if (typeof field !== "object" || field === null) return undefined;
    const v = field.value;
    return typeof v === "number" ? v : undefined;
  };
  const boolOf = (key: keyof DerivedInputsResponse): boolean | undefined => {
    if (!derived) return undefined;
    const field = derived[key];
    if (typeof field !== "object" || field === null) return undefined;
    const v = field.value;
    return typeof v === "boolean" ? v : undefined;
  };
  const strOf = (key: keyof DerivedInputsResponse): string | undefined => {
    if (!derived) return undefined;
    const field = derived[key];
    if (typeof field !== "object" || field === null) return undefined;
    const v = field.value;
    return typeof v === "string" && v.length > 0 ? v : undefined;
  };

  // A tiny placeholder for sections that depend on derived inputs while the
  // fetch is in flight. Matches the cards' own "Loading…" affordance.
  const loadingCard = (title: string) => (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-base font-semibold">{title}</div>
      <div className="text-sm text-muted-foreground mt-1">Loading…</div>
    </div>
  );

  return (
    <div className="container mx-auto px-4 py-6 max-w-6xl grid grid-cols-1 lg:grid-cols-[200px_1fr] gap-6">
      {/* Sticky section jump rail. */}
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
        {/* ── Visible by default: the spine + headline + verdict ── */}

        {/* Plain-language plan "story" lead — the spine of the page. */}
        <PlanStoryLead />

        {/* The most direct answer to "when can I retire?", surfaced
            above the P(solvent) verdict. */}
        <section id="when-can-i-retire" className="scroll-mt-6">
          <ExpectedRetirementAgeCard userId={USER_ID} />
        </section>

        <section id="verdict" className="scroll-mt-6 space-y-4">
          {/* Decision-surface scenario table — base/bull/bear at the
              permanent-equivalent spend basis with BL credited. The
              single-number hero below is the CI-gated point estimate. */}
          <ScenarioGridCard
            userId={USER_ID}
            retirementAge={numOf("retirement_age")}
          />
          <RuinProbabilityHero
            userId={USER_ID}
            retirementAge={numOf("retirement_age")}
            targetPSolvent={0.9}
            withdrawalPolicyId={policy}
            sigmaAnnual={calibratedSigma}
          />
        </section>

        {/* ── Collapsible depth: lazy-mounted, collapsed by default ── */}

        <section id="safety" className="scroll-mt-6">
          <CollapsibleSection title="Safety & withdrawal policy">
            <SafetyGatesPanel userId={USER_ID} />
            <SigmaCalibrationCard userId={USER_ID} />
            <WithdrawalPolicySelector
              initialPolicyId="guyton_klinger"
              onChange={(id) => setPolicy(id)}
            />
            {numOf("fx_usd_nis") === undefined ? (
              loadingCard("Stochastic FX")
            ) : (
              <StochasticFxCard initialFx={numOf("fx_usd_nis")!} horizonMonths={360} />
            )}
            {(() => {
              const age = numOf("current_age");
              return (
                <>
                  <GlidePathCard
                    currentAge={age === undefined ? undefined : Math.round(age)}
                    policy="vanguard_target_date"
                  />
                  {age === undefined ? (
                    loadingCard("Rebalancing alerts")
                  ) : (
                    <RebalancingAlertsCard userId={USER_ID} currentAge={Math.round(age)} />
                  )}
                </>
              );
            })()}
          </CollapsibleSection>
        </section>

        <section id="taxes" className="scroll-mt-6">
          <CollapsibleSection title="Taxes">
            <TaxBreakdownCard userId={USER_ID} />
            {(() => {
              const age = numOf("current_age");
              const firstDeposit = strOf("hishtalmut_first_deposit_date");
              if (age === undefined) return loadingCard("Hishtalmut eligibility timer");
              // first-deposit date is intake; pending → needs-intake note, never a
              // fabricated 2018-01-01 (the §3(e) 6yr timer must trace to a real date).
              if (firstDeposit === undefined) {
                return (
                  <div className="rounded-lg border border-border bg-card p-4">
                    <div className="text-base font-semibold">Hishtalmut eligibility</div>
                    <div className="text-sm text-muted-foreground mt-1">
                      First-deposit date needs intake — add it on the intake page to
                      compute the §3(e) 6-year tax-free timer.
                    </div>
                  </div>
                );
              }
              return (
                <HishtalmutTimerCard
                  userId={USER_ID}
                  firstDepositDate={firstDeposit}
                  currentAge={Math.round(age)}
                />
              );
            })()}
          </CollapsibleSection>
        </section>

        <section id="expenses" className="scroll-mt-6">
          <CollapsibleSection title="Expenses & life phases">
            <PhaseExpenseCard hasKids={boolOf("has_kids_under_18") ?? false} />
            {numOf("monthly_burn_nis") === undefined ? (
              loadingCard("Healthcare cost curve")
            ) : (
              <HealthcareCurveCard monthlyBurnNis={numOf("monthly_burn_nis")} />
            )}
            <HolisticTimelineCard userId={USER_ID} />
          </CollapsibleSection>
        </section>

        <section id="income" className="scroll-mt-6">
          <CollapsibleSection title="Income, real estate & insurance">
            {(() => {
              const monthlyNeed = numOf("monthly_need_nis");
              const taxable = numOf("taxable_balance_nis");
              const hishtalmut = numOf("hishtalmut_balance_nis");
              const kupatGemel = numOf("kupat_gemel_balance_nis");
              const pension = numOf("pension_balance_nis");
              const decReady =
                monthlyNeed !== undefined &&
                taxable !== undefined &&
                hishtalmut !== undefined;
              return (
                <>
                  {decReady ? (
                    <DecumulationOrderCard
                      monthlyNeedNis={monthlyNeed!}
                      taxableBalanceNis={taxable!}
                      hishtalmutBalanceNis={hishtalmut!}
                      kupatGemelBalanceNis={kupatGemel}
                    />
                  ) : (
                    loadingCard("Decumulation order")
                  )}
                  {pension !== undefined && monthlyNeed !== undefined ? (
                    <LumpVsAnnuityCard
                      pensionBalanceNis={pension}
                      mekademTypical={numOf("mekadem_typical")!}
                      monthlyExpenseNeedNis={monthlyNeed}
                    />
                  ) : (
                    loadingCard("Lump sum vs annuity")
                  )}
                </>
              );
            })()}

            {(() => {
              const residence = numOf("residence_value_nis");
              const mortgage = numOf("mortgage_balance_nis");
              // residence_value_nis is PENDING (no intake yet) → value:null.
              // Do NOT invent a residence value: show a needs-intake note so
              // equity (value − mortgage) is never computed off a fake number.
              const residencePending =
                derived !== null && derived.residence_value_nis.status === "pending";
              if (residencePending || (derived !== null && residence === undefined)) {
                return (
                  <div className="rounded-lg border border-border bg-card p-4">
                    <div className="text-base font-semibold">Real estate + mortgage</div>
                    <div className="text-sm text-muted-foreground mt-1">
                      Primary-residence value needs intake — add it on the intake
                      page to compute home equity. Mortgage balance:{" "}
                      {mortgage === undefined
                        ? "—"
                        : `₪${mortgage.toLocaleString()}`}
                      .
                    </div>
                  </div>
                );
              }
              if (residence === undefined || mortgage === undefined) {
                return loadingCard("Real estate + mortgage");
              }
              return (
                <RealEstateMortgageCard
                  primaryResidenceValueNis={residence}
                  mortgageBalanceNis={mortgage}
                  annualRate={numOf("mortgage_annual_rate")}
                  termMonths={numOf("mortgage_term_months")}
                />
              );
            })()}

            {(() => {
              const age = numOf("current_age");
              return age === undefined ? (
                loadingCard("Bituach Leumi (National Insurance)")
              ) : (
                <BituachLeumiCard
                  userId={USER_ID}
                  currentAge={Math.round(age)}
                  contributionHistoryYears={numOf("bl_contribution_history_years")}
                  /* spouseEligible: needs intake — defaults false (conservative) */
                  spouseEligible={false}
                />
              );
            })()}

            <MekademBand
              userId={USER_ID}
              /* TODO: derive fundId — not yet in derived-inputs */
              fundId="clal_pensia"
              balanceNis={numOf("pension_balance_nis")}
            />

            {(() => {
              const income = numOf("monthly_income_nis");
              const expenses = numOf("monthly_burn_nis");
              const dependents = numOf("dependents_count");
              const kids = boolOf("has_kids_under_18");
              const assets = numOf("net_worth_nis");
              const ready =
                income !== undefined &&
                expenses !== undefined &&
                dependents !== undefined &&
                kids !== undefined &&
                assets !== undefined;
              return ready ? (
                <InsuranceGapsCard
                  monthlyIncomeNis={income!}
                  monthlyExpensesNis={expenses!}
                  dependentsCount={dependents!}
                  hasKidsUnder18={kids!}
                  assetsNis={assets!}
                />
              ) : (
                loadingCard("Insurance gaps")
              );
            })()}
          </CollapsibleSection>
        </section>

        <section id="methodology" className="scroll-mt-6">
          <CollapsibleSection title="Methodology & sources">
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

            <DerivedInputsProvenancePanel data={derived} />
          </CollapsibleSection>
        </section>
      </div>
    </div>
  );
}
