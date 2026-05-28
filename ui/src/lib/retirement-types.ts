// TS counterparts to ``argosy/services/retirement/citations.py`` +
// ``argosy/services/retirement/sources.py``. Keep these in lockstep with
// the backend dataclasses — pydantic v2 + ``as_dict()`` serializes to
// this shape directly.
//
// Conventions:
//   - Optional fields use `?` (matching how as_dict strips Nones/empty lists).
//   - ``value`` and ``source_id`` are EXPLICITLY required-with-null because
//     the backend preserves those semantic Nones in JSON.

export interface ValueWithRationale {
  /** Null means "not enough data available yet". */
  value: number | string | null;
  unit: string;
  /** Null means derived/computed (no external citation). */
  source_id: string | null;
  rationale: string;
  alternatives_considered?: string[];
  /** YYYY-MM (or YYYY) when the value was current. */
  as_of_date?: string;
  /** Auto-stamped by resolver if as_of is stale, or intrinsic to the YAML. */
  freshness_warning?: string;
  confidence?: "high" | "medium" | "low";
}

export interface Source {
  id: string;
  title: string;
  url: string;
  as_of: string;
  kind: "official" | "research" | "derived" | "best_effort";
  notes?: string;
}

export interface SourcesResponse {
  sources: Record<string, Source>;
}

export interface MekademBandResponse {
  fund_id: string;
  typical: ValueWithRationale;
  low: ValueWithRationale;
  high: ValueWithRationale;
  /** Present when balance_nis query param is supplied. */
  annuity_monthly_nis_typical?: ValueWithRationale;
  annuity_monthly_nis_low?: ValueWithRationale;
  annuity_monthly_nis_high?: ValueWithRationale;
}

export type Verdict = "ON_TRACK" | "WARN" | "OFF_TRACK" | "UNCERTAIN";

export interface RuinProbabilityResponse {
  p_solvent_at_75: ValueWithRationale;
  p_solvent_at_85: ValueWithRationale;
  p_solvent_at_95: ValueWithRationale;
  p_solvent_at_95_ci_low: ValueWithRationale;
  p_solvent_at_95_ci_high: ValueWithRationale;
  target_p_solvent: ValueWithRationale;
  verdict: Verdict;
  suggested_action: ValueWithRationale;
}

export interface SigmaCalibrationResponse {
  sigma_annual: ValueWithRationale;
  portfolio_total_usd: ValueWithRationale;
  breakdown: Array<{
    asset_class: string;
    weight_pct: number;
    sigma: number;
    contribution: number;
    usd_value: number;
  }>;
}

export interface WithdrawalPolicy {
  id: "bengen_4pct" | "guyton_klinger" | "vpw" | "bucket";
  label: string;
  rationale: string;
  source_id: string;
}

export interface WithdrawalPoliciesResponse {
  policies: WithdrawalPolicy[];
}

// Wave 5/6/7 response shapes ─────────────────────────────────────────────

export interface TaxBreakdownResponse {
  gross: ValueWithRationale;
  net: ValueWithRationale;
  israeli_tax: ValueWithRationale;
  us_treaty_credit: ValueWithRationale;
  bituach_leumi_tax: ValueWithRationale;
  effective_rate: ValueWithRationale;
}

export interface HishtalmutEligibilityResponse {
  months_until_taxfree: ValueWithRationale;
  first_deposit_date: ValueWithRationale;
  six_yr_eligible: ValueWithRationale;
  age_67_eligible: ValueWithRationale;
  taxfree_now: ValueWithRationale;
  early_withdrawal_marginal_rate: ValueWithRationale;
}

export interface HishtalmutWithdrawalTaxResponse {
  tax: ValueWithRationale;
  /** 1 when the withdrawal is fully tax-free under the active path, 0 otherwise. */
  taxfree_now: number;
}

export interface DecumulationStep {
  order: number;
  account: string;
  monthly_draw_nis: ValueWithRationale;
  rationale: string;
}

export interface DecumulationResponse {
  steps: DecumulationStep[];
}

export interface LumpVsAnnuityResponse {
  recommendation: "take_annuity" | "take_lump" | "split";
  annuity_path: { monthly_annuity_nis: number; lifetime_npv_nis: number };
  lump_path: {
    initial_lump_nis: number;
    lifetime_npv_nis: number;
    balance_at_end_nis: number;
  };
  split_path: { annuity_monthly_nis: number; lifetime_npv_nis: number };
  rationale: ValueWithRationale;
}

export interface RealEstateResponse {
  primary_residence_value_nis: ValueWithRationale;
  mortgage_balance_nis: ValueWithRationale;
  equity_nis: ValueWithRationale;
  appreciation_annual: ValueWithRationale;
  illiquidity_haircut: ValueWithRationale;
  monthly_property_tax_nis: ValueWithRationale;
}

export interface MortgageScheduleRow {
  month: number;
  payment_nis: number | null;
  principal_paid_nis: number | null;
  interest_paid_nis: number | null;
  remaining_balance_nis: number | null;
}

export interface MortgageScheduleResponse {
  rows: MortgageScheduleRow[];
  term_months: number;
  total_interest_nis: number;
}

export interface PartnerResponse {
  partner: {
    age_years: ValueWithRationale;
    monthly_income_nis: ValueWithRationale;
    pension_balance_nis: ValueWithRationale;
    retirement_age: ValueWithRationale;
    is_eligible_for_bl_supplement: ValueWithRationale;
  } | null;
  household_retire_ready_age: ValueWithRationale;
}

export interface SeveranceResponse {
  accrued_pizurim_nis: ValueWithRationale;
  withdrawn_history_nis: ValueWithRationale;
  annuitization_probability: ValueWithRationale;
  tax_treatment: ValueWithRationale;
  effective_pension_for_annuity_nis: ValueWithRationale;
}

export interface InsuranceGap {
  insurance_type: "life" | "disability" | "ltc" | "health_supplementary";
  recommended_coverage: ValueWithRationale;
  actual_coverage: ValueWithRationale;
  gap: ValueWithRationale;
  suggested_action: ValueWithRationale;
}

export interface InsuranceGapsResponse {
  gaps: InsuranceGap[];
}

export interface PhaseExpenseRow {
  start_age: number;
  end_age: number;
  label: string;
  monthly_multiplier: ValueWithRationale;
  inflation_premium: ValueWithRationale;
}

export interface PhaseExpensesResponse {
  phases: PhaseExpenseRow[];
}

export interface LifecycleIncomeRow {
  age: number;
  event_type: string;
  monthly_impact_nis: ValueWithRationale;
  probability: ValueWithRationale;
  rationale: string;
}

export interface LifecycleIncomeResponse {
  events: LifecycleIncomeRow[];
}

export interface HealthcareCurveRow {
  age: number;
  monthly_cost_nis: ValueWithRationale;
}

export interface HealthcareCurveResponse {
  curve: HealthcareCurveRow[];
  share_of_burn_at_70: ValueWithRationale | null;
}

export interface FxBandResponse {
  horizon_months: number;
  initial_fx: number;
  bands: {
    p10: ValueWithRationale;
    p25: ValueWithRationale;
    p50: ValueWithRationale;
    p75: ValueWithRationale;
    p90: ValueWithRationale;
  };
}

export type GateStatus = "PASS" | "WARN" | "FAIL";

export interface GateVerdict {
  gate_id: "nra_estate" | "emergency_liquidity" | "conflict_scenario";
  status: GateStatus;
  value: ValueWithRationale;
  threshold: ValueWithRationale;
  suggested_action: ValueWithRationale;
  detail_summary: string;
}

export interface SafetyGatesResponse {
  gates: GateVerdict[];
}

export interface BLStipendResponse {
  monthly_nis: ValueWithRationale;
  monthly_nis_low: ValueWithRationale;
  monthly_nis_high: ValueWithRationale;
  eligibility_age: ValueWithRationale;
  contribution_history_factor: ValueWithRationale;
  spouse_supplement_applied: ValueWithRationale;
  sensitivity_levers: Array<{
    name: string;
    delta_nis_per_mo: number;
    source_id: string;
  }>;
}
