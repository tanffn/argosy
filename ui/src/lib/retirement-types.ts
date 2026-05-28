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
