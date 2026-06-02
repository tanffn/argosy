"""Pydantic types for the baseline plan distillate.

Per SDD §6.10 / spec §3: the distillate captures durable principles +
targets-as-stated; explicitly drops time-stamped numbers (current
portfolio %, FX rate, share counts, dated tranche schedules).

Each item carries a ``source_section`` pointer back to the heading in
the imported plan markdown for click-through provenance.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Phase 4 — re-use the Phase 3 Assumption type rather than redefining
# it. The distillate's ``plan_assumptions`` records assumptions the
# USER baked into the imported plan (e.g. "real return 4%, fee drag
# 30 bps"); downstream synthesis Sections may also produce Assumption
# rows when they cite inference-kind sources (§6 evidence contract).
from argosy.agents.plan_synthesizer_types import Assumption


# Allowed unit values for ``Target.unit``. Kept as a Literal for
# pydantic validation rather than a free-form string.
TargetUnit = Literal[
    "pct_of_portfolio",
    "pct_of_net_worth",
    "pct_of_liquid",
    "usd",
    "nis",
    "shares",
    "ratio",
    "years",
]


class Goal(BaseModel):
    """A durable goal extracted from the plan.

    Examples: retirement target year, target annual income, FI status,
    employment horizon. Goals are durable (years, not months) and rarely
    revised between syntheses.
    """

    label: str
    value: str
    rationale: str = ""
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class Principle(BaseModel):
    """An investment-philosophy principle from the plan.

    Examples: UCITS-first for estate safety, real-returns framework,
    NIS salary covers NIS expenses (natural hedge), concentration is
    the load-bearing risk.
    """

    label: str
    rationale: str = ""
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class DecisionRule(BaseModel):
    """A decision rule the user has committed to.

    Examples: bracket-aware RSU sales, gap-weighted deployment, no
    Defensive above cap, never panic-convert NIS<->USD.
    """

    label: str
    rule: str
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class Target(BaseModel):
    """A numeric target with explicit as-of stamping.

    Examples: NVDA -> 15%, defensive 5-8%, Core 20-25%, Growth 15-20%.
    The ``stated_at`` and ``revisit_after`` dates make the time-bound
    nature of the value explicit so consumers can age-down the
    recommendation as needed.
    """

    label: str
    value: float
    unit: TargetUnit
    stated_at: date
    revisit_after: date
    rationale: str = ""
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class Constraint(BaseModel):
    """An operational constraint the user has opted in to.

    Examples: no consolidate brokers, UCITS preferred, limited account
    capped at $1k, speculation max % cap.
    """

    label: str
    detail: str
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


# ---------------------------------------------------------------------------
# Phase 4 — P0/P1 distillate-field types (v3.1 plan §7.1).
#
# Field naming contract: every PlanDistillate field added below
# matches a key in
# ``argosy.quality.canonical_sections.DISTILLATE_FIELD_TO_SECTION_ID``
# exactly. The Phase 0 binding gate composes citations of the form
# ``distillate.<field_name>[<idx>]`` against the bound section_id;
# mismatched names would silently bypass the gate.
#
# ``ConfigDict(extra="ignore")`` on new classes so P2/P3 sub-field
# additions in a follow-up phase don't break rolling deploys.
# ---------------------------------------------------------------------------


class CashflowPhase(BaseModel):
    """A life-phase cashflow delta the user has declared in the plan.

    Examples: "kids leave home -> -₪40k/yr expense reduction starting
    age 58", "wedding bulge ₪50k/yr ages 60-62", "car replacement
    every 5 years ₪150k". Phases are open-ended on ``end_age=None``
    (retirement-onset reductions that persist).
    """

    model_config = ConfigDict(extra="ignore")

    phase_label: str
    start_age: int
    end_age: int | None = None
    annual_delta_nis: Decimal
    narrative: str = ""
    source_locator: str = ""


class GrantRow(BaseModel):
    """One RSU / option grant from the user's equity-comp schedule.

    Required for per-grant tax-track resolution (§102 capital vs
    ordinary, §3i, ESOP). ``holding_clock_end`` matters for §102
    capital-track 24-month preferential-rate eligibility.
    """

    model_config = ConfigDict(extra="ignore")

    grant_id: str
    grant_date: date
    share_count: Decimal
    grant_price_usd: Decimal | None = None
    vest_schedule: str
    track: Literal["102_capital", "102_ordinary", "3i", "esop", "other"]
    trustee: str | None = None
    holding_clock_end: date | None = None


class BridgeRung(BaseModel):
    """One rung of the FI-bridge waterfall — the sequence of accounts
    drawn on between early retirement and statutory pension age."""

    model_config = ConfigDict(extra="ignore")

    rung_label: str
    start_age: int
    end_age: int | None = None
    source_account: Literal[
        "keren_hishtalmut",
        "kupot_gemel",
        "executive_insurance",
        "pensia",
        "portfolio_drawdown",
        "employment",
        "other",
    ]
    annual_nis: Decimal
    tax_status: Literal["tax_free", "ordinary_income", "capital_gains", "mixed"]
    notes: str = ""


class WithdrawalYearRow(BaseModel):
    """One year of the projected withdrawal schedule. Gross / tax /
    net split lets the FM agent and tax-plan section share a single
    source of truth for retirement-income mechanics."""

    model_config = ConfigDict(extra="ignore")

    year: int
    age: int
    source_account: str
    gross_nis: Decimal
    tax_withheld_nis: Decimal
    net_nis: Decimal
    running_balance_nis: Decimal
    notes: str = ""


class MonteCarloGrid(BaseModel):
    """Single-shot Monte Carlo / sensitivity grid from the plan."""

    model_config = ConfigDict(extra="ignore")

    paths: int
    success_definition: str
    success_rate: float = Field(..., ge=0.0, le=1.0)
    return_assumption_pct: Decimal
    fee_drag_pct: Decimal
    sensitivity_rows: list[dict[str, Decimal]] = Field(default_factory=list)


class TaxYearRow(BaseModel):
    """One year of the projected tax schedule. Surtax band is split
    out because IL surtax kicks in at the ~₪721k threshold and the
    cliff effect needs its own line for downstream optimization."""

    model_config = ConfigDict(extra="ignore")

    year: int
    gross_income_nis: Decimal
    surtax_band_nis: Decimal
    effective_rate_pct: Decimal
    marginal_rate_pct: Decimal
    notes: str = ""


class CrossBorderPlan(BaseModel):
    """Multi-jurisdictional posture: US-person status per household
    member, US-situs exposure, NRA estate-tail dollars, forms
    calendar, PFIC × estate-tax resolution per holder."""

    model_config = ConfigDict(extra="ignore")

    household_us_persons: list[str] = Field(default_factory=list)
    us_situs_exposure_usd: Decimal
    nra_estate_tail_usd: Decimal
    forms_calendar: list[dict[str, str]] = Field(default_factory=list)
    pfic_estate_resolution_per_holder: dict[str, str] = Field(
        default_factory=dict
    )


class REPlanItem(BaseModel):
    """One real-estate plan item — what to do with each property over
    the planning horizon."""

    model_config = ConfigDict(extra="ignore")

    property_label: str
    action: Literal["hold", "refinance", "sell", "rent_out"]
    action_date: date | None = None
    expected_outcome_nis: Decimal
    notes: str = ""


class FXStrategyPlan(BaseModel):
    """The user's NIS↔USD conversion strategy."""

    model_config = ConfigDict(extra="ignore")

    base_currency: str
    target_usd_pct: Decimal
    conversion_cadence: Literal[
        "monthly",
        "quarterly",
        "opportunistic",
        "threshold_driven",
    ]
    threshold_rule: str | None = None
    broker: Literal["bank", "ibkr", "other"]
    annual_savings_nis: Decimal | None = None


class ETFRefRow(BaseModel):
    """One row of the ETF reference table."""

    model_config = ConfigDict(extra="ignore")

    asset_class: str
    ticker: str
    domicile: Literal["US", "IE", "DE", "LU", "UK", "IL", "other"]
    ter_bps: Decimal
    estate_safe: bool
    rating: str


# ---------------------------------------------------------------------------


class PlanDistillate(BaseModel):
    """Compressed structured extract of a baseline plan.

    Target rendered size: 1500-2500 tokens. The only representation of
    the baseline that downstream synthesis ever consumes; the full
    ``raw_markdown`` is preserved for forensic / "show me the source"
    lookups but is never injected into agent prompts.

    Exclusions enforced by the distiller's system prompt:
      - Current portfolio percentages
      - Current FX rates
      - Specific dollar amounts at point-in-time
      - Dated tranche schedules
      - Share counts
      - Implementation roadmap "next 30/90 days" sections
    """

    plan_label: str
    distilled_at_iso: str  # ISO-8601 UTC

    goals: list[Goal] = Field(default_factory=list)
    principles: list[Principle] = Field(default_factory=list)
    risk_priorities: list[str] = Field(
        default_factory=list,
        description="Ordered list of top risks; first item dominates."
    )
    decision_rules: list[DecisionRule] = Field(default_factory=list)
    targets: list[Target] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    stress_tolerance: str = ""

    # ------------------------------------------------------------------
    # Phase 4 — P0 typed fields
    # ------------------------------------------------------------------
    # All default-empty so legacy distillate JSON loads unchanged.
    # Field names match argosy.quality.canonical_sections.DISTILLATE_FIELD_TO_SECTION_ID.
    plan_assumptions: list[Assumption] = Field(default_factory=list)
    cashflow_phases: list[CashflowPhase] = Field(default_factory=list)
    equity_comp_grants: list[GrantRow] = Field(default_factory=list)
    unmapped_sections: list[str] = Field(
        default_factory=list,
        description=(
            "Verbatim H2 titles from the source plan that didn't fit "
            "any typed bucket. Signals to the synth (and to the UI) "
            "that user-authored content was not structurally ingested "
            "and must be surfaced or explicitly omitted with rationale."
        ),
    )

    # ------------------------------------------------------------------
    # Phase 4 — P1 typed fields
    # ------------------------------------------------------------------
    fi_bridge: list[BridgeRung] = Field(default_factory=list)
    withdrawal_schedule: list[WithdrawalYearRow] = Field(default_factory=list)
    monte_carlo_grid: MonteCarloGrid | None = None
    tax_schedule: list[TaxYearRow] = Field(default_factory=list)
    cross_border: CrossBorderPlan | None = None
    real_estate_plan: list[REPlanItem] = Field(default_factory=list)
    fx_strategy: FXStrategyPlan | None = None
    etf_reference: list[ETFRefRow] = Field(default_factory=list)


__all__ = [
    # Original 7-bucket schema
    "Goal",
    "Principle",
    "DecisionRule",
    "Target",
    "TargetUnit",
    "Constraint",
    "PlanDistillate",
    # Phase 4 P0/P1 sub-types
    "CashflowPhase",
    "GrantRow",
    "BridgeRung",
    "WithdrawalYearRow",
    "MonteCarloGrid",
    "TaxYearRow",
    "CrossBorderPlan",
    "REPlanItem",
    "FXStrategyPlan",
    "ETFRefRow",
]
