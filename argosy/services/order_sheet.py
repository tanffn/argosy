"""Canonical, executable portfolio decision artifact.

An :class:`OrderSheet` is the boundary between Argosy's judgment team and the
deterministic money/execution floor.  Review, discovery and allocation may use
LLMs, but no recommendation is actionable until it can be represented here and
passes :func:`validate_order_sheet`.

The contract is deliberately richer than ``AllocationProposal`` and
``position_stances``: a word such as BUY is not an order.  Every action carries
quantity, notional, venue, current evidence, a dated catalyst, a falsifier, an
outcome clock, tax economics (for sales), and priced constraints.  The sheet is
one list; ``no_action`` records the deliberate omissions from the same run.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class OrderAction(StrEnum):
    BUY = "BUY"
    ADD = "ADD"
    SELL = "SELL"
    TRIM = "TRIM"


class ThesisType(StrEnum):
    COMPOUNDER = "compounder"
    VALUE = "value"
    INCOME = "income"
    DIVERSIFIER = "diversifier"
    CONVEXITY = "convexity"


class VoiceVerdict(BaseModel):
    """One judgment path's verdict with explicit decision-run authority.

    Prior plan/review output remains attached for telemetry and self-audit, but
    only voices produced for the current decision run can veto its order sheet.
    This prevents yesterday's unresolved audit trail from masquerading as a
    second current decision while preserving it verbatim.
    """

    source: str = Field(min_length=1)
    verdict: str = Field(
        min_length=1,
        description="Raw upstream label for prior_context; current_run requires BUY/ADD/HOLD/TRIM/SELL.",
    )
    as_of: datetime
    rationale: str = Field(min_length=1)
    decision_scope: Literal["current_run", "prior_context"] = "current_run"

    @model_validator(mode="after")
    def _current_action_vocabulary(self) -> VoiceVerdict:
        if not self.verdict.strip():
            raise ValueError("A voice must retain a nonblank verdict")
        if (self.decision_scope == "current_run"
                and self.verdict not in {"BUY", "ADD", "HOLD", "TRIM", "SELL"}):
            raise ValueError("Current-run verdict must be BUY/ADD/HOLD/TRIM/SELL")
        return self


class MarketEvidence(BaseModel):
    """Facts that make an order eligible at a particular point in time."""

    price_usd: float = Field(gt=0)
    price_as_of: datetime
    price_source: str = Field(min_length=1)
    market_cap_usd: float | None = Field(default=None, gt=0)
    market_cap_as_of: datetime | None = None
    market_cap_source: str | None = None
    incorporation_country: str | None = None
    incorporation_as_of: datetime | None = None
    incorporation_source: str | None = None


class DatedCatalyst(BaseModel):
    description: str = Field(min_length=1)
    due_date: date


class OutcomeExpectation(BaseModel):
    """The prediction that the evaluator must revisit on ``due_date``."""

    statement: str = Field(min_length=1)
    due_date: date
    success_measure: str = Field(min_length=1)


class OutcomeScenario(BaseModel):
    """One authored terminal-value case used to size a convex position.

    The fleet authors probabilities and terminal multiples; deterministic code
    only verifies conservation and computes the probability-weighted result.
    """

    label: str = Field(min_length=1)
    probability_pct: float = Field(gt=0, le=100)
    terminal_multiple: float = Field(ge=0)
    terminal_date: date | None = None
    rationale: str = Field(min_length=1)


_BUY_CGT_RATE = 0.25


def _scenario_metrics(
    scenarios: list[OutcomeScenario], *, start_date: date
) -> dict[str, float | date | str | None]:
    """Pure arithmetic over fleet-authored scenarios.

    Gains are reduced by the Israeli headline CGT rate; losses receive no tax
    value because timing/usability is not guaranteed. This is deliberately a
    conservative display metric, not tax advice or an investment gate.
    """
    if not scenarios:
        return {}
    weighted = sum(s.probability_pct * s.terminal_multiple for s in scenarios) / 100.0
    after_tax = sum(
        s.probability_pct
        * (
            1.0 + (s.terminal_multiple - 1.0) * (1.0 - _BUY_CGT_RATE)
            if s.terminal_multiple > 1.0
            else s.terminal_multiple
        )
        for s in scenarios
    ) / 100.0
    ordered = sorted(scenarios, key=lambda s: s.terminal_multiple)
    cumulative = 0.0
    median = ordered[-1].terminal_multiple
    for scenario in ordered:
        cumulative += scenario.probability_pct
        if cumulative >= 50.0:
            median = scenario.terminal_multiple
            break
    dates = {s.terminal_date for s in scenarios if s.terminal_date is not None}
    terminal = next(iter(dates)) if len(dates) == 1 else None
    years = (
        max((terminal - start_date).days / 365.25, 1 / 365.25)
        if terminal is not None and terminal > start_date
        else None
    )
    annualized = (
        (weighted ** (1.0 / years) - 1.0) * 100.0
        if years is not None and weighted >= 0
        else None
    )
    after_tax_annualized = (
        (after_tax ** (1.0 / years) - 1.0) * 100.0
        if years is not None and after_tax >= 0
        else None
    )
    return {
        "probability_weighted_multiple": weighted,
        "scenario_terminal_date": terminal,
        "scenario_horizon_years": years,
        "annualized_expected_return_pct": annualized,
        "after_tax_expected_multiple": after_tax,
        "after_tax_annualized_expected_return_pct": after_tax_annualized,
        "median_terminal_multiple": median,
        "probability_of_loss_pct": sum(
            s.probability_pct for s in scenarios if s.terminal_multiple < 1.0
        ),
        "probability_of_near_wipeout_pct": sum(
            s.probability_pct for s in scenarios if s.terminal_multiple <= 0.2
        ),
        "tax_assumption": "25% CGT on gains; no value assigned to loss offsets",
    }


class CandidateComparison(BaseModel):
    """Fleet-authored explanation of a discovery finalist's disposition.

    Raw rank/grade fields make coarse discovery stages visible; ``why`` is the
    comparative judgment that selects or rejects the name.  The allocation
    verifier cross-checks the raw fields against the packet and never authors
    the decision itself.
    """

    ticker: str = Field(min_length=1)
    selection: Literal["SELECTED", "NOT_SELECTED"]
    radar_rank: int | None = Field(default=None, ge=1)
    radar_score: float | None = None
    research_verdict: Literal["BUY", "HOLD", "TRIM", "SELL", "ABSTAIN", "WATCH", "PASS"] | None = None
    research_conviction: Literal["HIGH", "MED", "LOW"] | None = None
    evidence_fresh_as_of: datetime
    key_advantage: str = Field(min_length=1)
    key_risk: str = Field(min_length=1)
    why: str = Field(min_length=1)
    outcome_scenarios: list[OutcomeScenario] = Field(default_factory=list)
    probability_confidence: Literal["LOW", "MED", "HIGH"] | None = None
    probability_basis: str | None = None
    probability_weighted_multiple: float | None = Field(default=None, ge=0)
    scenario_terminal_date: date | None = None
    scenario_horizon_years: float | None = Field(default=None, gt=0)
    annualized_expected_return_pct: float | None = None
    after_tax_expected_multiple: float | None = Field(default=None, ge=0)
    after_tax_annualized_expected_return_pct: float | None = None
    median_terminal_multiple: float | None = Field(default=None, ge=0)
    probability_of_loss_pct: float | None = Field(default=None, ge=0, le=100)
    probability_of_near_wipeout_pct: float | None = Field(default=None, ge=0, le=100)
    tax_assumption: str | None = None
    recommended_position_usd: float | None = Field(default=None, ge=0)
    smaller_position_usd: float | None = Field(default=None, ge=0)
    why_not_smaller: str | None = None
    larger_position_usd: float | None = Field(default=None, ge=0)
    why_not_larger: str | None = None
    split_considered: bool | None = None
    split_why: str | None = None
    # Retained for reading older artifacts. New UI surfaces the structured
    # alternatives above, so model-authored arithmetic never becomes display truth.
    sizing_why: str | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_fleet_field_aliases(cls, value: object) -> object:
        """Normalize common fleet JSON labels without weakening requirements.

        Deployment authors use unconstrained JSON because the bundled CLI is
        unreliable with nested schemas.  The semantic fields remain mandatory,
        but harmless naming variants must not discard a completed comparison.
        """

        if not isinstance(value, dict):
            return value
        data = dict(value)
        aliases = {
            "ticker": ("symbol",),
            "selection": ("status", "disposition"),
            "radar_rank": ("rank",),
            "radar_score": ("score",),
            "research_verdict": ("verdict", "fleet_verdict"),
            "research_conviction": ("conviction", "fleet_conviction"),
            "evidence_fresh_as_of": (
                "fresh_as_of",
                "freshness_timestamp",
                "evidence_as_of",
            ),
            "key_advantage": ("strongest_advantage", "advantage"),
            "key_risk": ("load_bearing_risk", "risk"),
            "why": ("comparative_reason", "reason", "rationale"),
            "recommended_position_usd": (
                "recommended_amount_usd",
                "position_usd",
                "sizing_amount_usd",
            ),
            "sizing_why": ("sizing_reason", "amount_reason"),
            "smaller_position_usd": ("smaller_alternative_usd",),
            "why_not_smaller": ("smaller_alternative_reason",),
            "larger_position_usd": ("larger_alternative_usd", "considered_position_usd"),
            "why_not_larger": ("larger_alternative_reason", "why_zero"),
            "split_why": ("split_reason",),
        }
        for canonical, alternatives in aliases.items():
            if canonical in data:
                continue
            for alternative in alternatives:
                if alternative in data:
                    data[canonical] = data[alternative]
                    break

        selection = data.get("selection")
        if isinstance(selection, bool):
            data["selection"] = "SELECTED" if selection else "NOT_SELECTED"
        elif selection is not None:
            normalized = str(selection).strip().upper().replace("-", "_").replace(" ", "_")
            if normalized in {"CHOSEN", "WINNER"}:
                normalized = "SELECTED"
            elif normalized in {"REJECTED", "NOT_CHOSEN", "PASSED_OVER", "LOSER"}:
                normalized = "NOT_SELECTED"
            data["selection"] = normalized
        return data

    @model_validator(mode="after")
    def derive_probability_weighted_multiple(self) -> CandidateComparison:
        if self.outcome_scenarios:
            for field_name, value in _scenario_metrics(
                self.outcome_scenarios,
                start_date=self.evidence_fresh_as_of.date(),
            ).items():
                setattr(self, field_name, value)
        return self

    @model_validator(mode="after")
    def normalize_ticker(self) -> CandidateComparison:
        self.ticker = self.ticker.strip().upper()
        return self

    def sizing_structure_errors(self) -> list[str]:
        """Validate the arithmetic/judgment boundary for finalist sizing.

        Dollar alternatives are machine-readable. Their reasons are qualitative
        so the UI never publishes model-invented portfolio arithmetic.
        """
        amount = self.recommended_position_usd
        if amount is None:
            return ["recommended_position_usd is missing"]
        errors: list[str] = []
        if amount > 0:
            if self.smaller_position_usd is None or not self.smaller_position_usd < amount:
                errors.append("smaller_position_usd must be below the recommendation")
            if not (self.why_not_smaller or "").strip():
                errors.append("why_not_smaller is missing")
        elif self.smaller_position_usd is not None:
            errors.append("a zero recommendation must leave smaller_position_usd null")
        if self.larger_position_usd is None or not self.larger_position_usd > amount:
            errors.append("larger_position_usd must be above the recommendation")
        if not (self.why_not_larger or "").strip():
            errors.append("why_not_larger is missing")
        if self.split_considered is None or not (self.split_why or "").strip():
            errors.append("split_considered and split_why are required")
        for field_name, value in (
            ("why_not_smaller", self.why_not_smaller),
            ("why_not_larger", self.why_not_larger),
            ("split_why", self.split_why),
        ):
            if value and (any(ch.isdigit() for ch in value) or any(ch in "$%₪€£" for ch in value)):
                errors.append(f"{field_name} must be qualitative; arithmetic belongs in structured fields")
        return errors


class TaxImpact(BaseModel):
    """After-tax economics for one sell/trim line."""

    gross_proceeds_usd: float = Field(ge=0)
    calculation_basis: Literal["capital_gain", "trusted_tax_engine"] = "capital_gain"
    cost_basis_usd: float | None = Field(default=None, ge=0)
    taxable_gain_usd: float | None = Field(default=None, ge=0)
    capital_income_usd: float | None = Field(default=None, ge=0)
    ordinary_income_usd: float | None = Field(default=None, ge=0)
    effective_tax_rate: float = Field(ge=0, le=1)
    estimated_tax_usd: float = Field(ge=0)
    net_proceeds_usd: float = Field(ge=0)
    method: str = Field(min_length=1)
    authoritative: bool
    as_of: datetime
    evidence_as_of: date | None = None
    evidence_age_days: int | None = Field(default=None, ge=0)
    evidence_max_age_days: int | None = Field(default=None, ge=0)
    evidence_expires_on: date | None = None


class ConstraintCost(BaseModel):
    """A priced trade-off, never a hidden boolean prohibition.

    ``basis_value_usd`` is the value exposed to the constraint.  For US estate
    tax this must be projected FMV at death/event, not purchase cost.
    """

    constraint: str = Field(min_length=1)
    estimated_cost_usd: float = Field(ge=0)
    basis_value_usd: float = Field(ge=0)
    valuation_basis: Literal[
        "current_fmv",
        "estimated_fmv_at_event",
        "tax_liability",
        "commission",
        "other",
    ]
    as_of: datetime
    method: str = Field(min_length=1)


class StagedExecutionPlan(BaseModel):
    """Current tranche plus the mandatory re-decision boundary."""

    style: Literal["staged_tranche"] = "staged_tranche"
    execute_by: date
    next_review_date: date
    tranche_reason: str = Field(min_length=1)
    maximum_current_tranche_usd: float = Field(gt=0)
    remaining_glide_quarters: int = Field(ge=1)
    next_waypoint_date: date
    next_waypoint_weight_pct: float | None = Field(default=None, ge=0, le=100)
    shares_to_sell_by_next_waypoint: int = Field(ge=0)
    glide_base_shares_to_sell_by_next_waypoint: int = Field(ge=0)
    tax_denominator_adjustment_shares: int = Field(ge=0)
    weight_metric: Literal["direct_nvda"] = "direct_nvda"
    estimated_post_trade_weight_pct: float | None = Field(
        default=None, ge=0, le=100
    )
    estimated_post_trade_direct_nvda_weight_pct: float | None = Field(
        default=None, ge=0, le=100
    )
    estimated_post_trade_effective_nvda_weight_pct: float | None = Field(
        default=None, ge=0, le=100
    )
    clips: list[ExecutionClip] = Field(default_factory=list)
    reassess_after_fill: bool = True


class ExecutionClip(BaseModel):
    """One dated step toward a waypoint; only the first clip is executable."""

    sequence: int = Field(ge=1)
    target_date: date
    shares: int = Field(gt=0)
    executable_now: bool = False
    requires_reprice_and_reapproval: bool = True


class OrderLine(BaseModel):
    symbol: str = Field(min_length=1)
    action: OrderAction
    shares: float = Field(gt=0)
    notional_usd: float = Field(gt=0)
    authored_notional_usd: float | None = Field(default=None, gt=0)
    quantity_increment: float = Field(default=1.0, gt=0)
    venue: str = Field(min_length=1)
    instrument_type: Literal["stock", "etf", "option"] = "stock"
    thesis: str = Field(min_length=1)
    thesis_type: ThesisType
    falsifier: str = Field(min_length=1)
    catalyst: DatedCatalyst
    expectation: OutcomeExpectation
    evidence: MarketEvidence
    stance_source: Literal["portfolio_review", "discovery", "rebalance", "funding_switch"]
    voices: list[VoiceVerdict] = Field(min_length=1)
    post_trade_weight_pct: float = Field(ge=0, le=100)
    expected_upside_multiple: float | None = Field(default=None, gt=0)
    outcome_scenarios: list[OutcomeScenario] = Field(default_factory=list)
    probability_confidence: Literal["LOW", "MED", "HIGH"] | None = None
    probability_basis: str | None = None
    probability_weighted_multiple: float | None = Field(default=None, ge=0)
    scenario_terminal_date: date | None = None
    scenario_horizon_years: float | None = Field(default=None, gt=0)
    annualized_expected_return_pct: float | None = None
    after_tax_expected_multiple: float | None = Field(default=None, ge=0)
    after_tax_annualized_expected_return_pct: float | None = None
    median_terminal_multiple: float | None = Field(default=None, ge=0)
    probability_of_loss_pct: float | None = Field(default=None, ge=0, le=100)
    probability_of_near_wipeout_pct: float | None = Field(default=None, ge=0, le=100)
    tax_assumption: str | None = None
    expected_portfolio_contribution_pct: float | None = None
    capital_at_risk_pct: float | None = Field(default=None, ge=0)
    tax: TaxImpact | None = None
    constraint_costs: list[ConstraintCost] = Field(default_factory=list)
    estate_situs: Literal["US", "non_US", "unknown"] = "unknown"
    staged_execution: StagedExecutionPlan | None = None

    @model_validator(mode="after")
    def normalize_symbol(self) -> OrderLine:
        self.symbol = self.symbol.strip().upper()
        if self.outcome_scenarios:
            for field_name, value in _scenario_metrics(
                self.outcome_scenarios,
                start_date=self.evidence.price_as_of.date(),
            ).items():
                setattr(self, field_name, value)
        return self


class NoActionLine(BaseModel):
    symbol: str = Field(min_length=1)
    verdict: Literal["HOLD", "NO_ACTION"] = "NO_ACTION"
    reason: str = Field(min_length=1)
    voices: list[VoiceVerdict] = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_symbol(self) -> NoActionLine:
        self.symbol = self.symbol.strip().upper()
        return self


class FundingSummary(BaseModel):
    new_cash_usd: float = Field(ge=0)
    gross_sell_proceeds_usd: float = Field(ge=0)
    sell_tax_usd: float = Field(ge=0)
    sell_costs_usd: float = Field(ge=0)
    reserve_usd: float = Field(ge=0)
    rounding_residual_usd: float = Field(default=0.0, ge=0)
    available_to_buy_usd: float = Field(ge=0)


class PendingResearch(BaseModel):
    """A non-executable decision, with one shared (not per-alternative) reserve."""

    tickers: list[str] = Field(min_length=1)
    disagreement: str = Field(min_length=1)
    missing_evidence: str = Field(min_length=1)
    research_question: str = Field(min_length=1)
    next_review_date: date
    reserved_usd: float = Field(ge=0, allow_inf_nan=False)
    independence_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_tickers(self) -> PendingResearch:
        self.tickers = sorted({s.strip().upper() for s in self.tickers if s.strip()})
        if not self.tickers:
            raise ValueError("pending research requires named tickers")
        return self


def pending_research_errors(items, *, reserve_usd, action_symbols, as_of):
    """Contract/arithmetic only; independence is judged by the blind team."""
    errors = []
    seen = set()
    for item in items:
        symbols = set(item.tickers)
        if symbols & (seen | set(action_symbols)):
            errors.append("pending research tickers must be unique and cannot be executable actions")
        seen.update(symbols)
        if item.next_review_date <= as_of:
            errors.append("pending research requires a future next_review_date")
    if sum(item.reserved_usd for item in items) > reserve_usd + 0.01:
        errors.append("research reserves exceed the sheet's conserved cash reserve")
    return errors


class ReviewObjectionRecord(BaseModel):
    round: int = Field(ge=1)
    lens: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    concern: str = Field(min_length=1)
    severity: Literal["block", "warn"]
    impact: Literal[
        "advisory_only",
        "changes_amount",
        "changes_ticker",
        "adds_omitted_candidate",
        "rejects_trade",
    ]
    proposed_amount_usd: float = Field(ge=0)
    recommended_amount_usd: float | None = Field(default=None, ge=0)
    recommended_ticker: str | None = None
    status: Literal["advisory", "resolved_by_re_review", "unresolved", "deferred_for_research"]


class ReviewResolution(BaseModel):
    rounds: int = Field(ge=1)
    reviewers_ran: int = Field(ge=0)
    reviewers_expected: int = Field(ge=0)
    one_voice: bool
    summary: str = Field(min_length=1)
    objections: list[ReviewObjectionRecord] = Field(default_factory=list)
    separation_reviewed: bool = False


class OrderSheet(BaseModel):
    """One conflict-free, fully funded set of actions for one decision run."""

    user_id: str = Field(min_length=1)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    horizon_years_min: int = Field(ge=1)
    horizon_years_max: int = Field(ge=1)
    freshness_days: int = Field(default=3, ge=0)
    portfolio_symbols: list[str] = Field(default_factory=list)
    funding: FundingSummary
    # A run with zero verified funding and only execution-blocked signals is a
    # legitimate first-class NO-ACTION artifact. Funding conservation below
    # still rejects an empty action list whenever cash/proceeds are available.
    lines: list[OrderLine] = Field(default_factory=list)
    no_action: list[NoActionLine] = Field(min_length=1)
    candidate_comparisons: list[CandidateComparison] = Field(default_factory=list)
    pending_research: list[PendingResearch] = Field(default_factory=list)
    review_resolution: ReviewResolution | None = None
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def horizon_is_ordered(self) -> OrderSheet:
        if self.horizon_years_max < self.horizon_years_min:
            raise ValueError("horizon_years_max must be >= horizon_years_min")
        return self


class SheetFailure(BaseModel):
    code: str
    detail: str


class SheetValidation(BaseModel):
    valid: bool
    failures: list[SheetFailure] = Field(default_factory=list)
    buy_total_usd: float
    net_sell_proceeds_usd: float


def _age_days(now: datetime, observed: datetime) -> float:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return (now - observed).total_seconds() / 86_400.0


def _voice_failure(symbol: str, voices: list[VoiceVerdict]) -> SheetFailure | None:
    verdicts = {v.verdict for v in voices if v.decision_scope == "current_run"}
    if len(verdicts) > 1:
        return SheetFailure(
            code="one_voice_conflict",
            detail=f"{symbol}: judgment paths disagree: {sorted(verdicts)}",
        )
    return None


def validate_order_sheet(sheet: OrderSheet) -> SheetValidation:
    """Validate the arithmetic floor and the owner's explicit policies.

    This does not choose investments.  It proves that the team's judgment is
    coherent, current, sized consistently with its thesis, and executable.
    """

    failures: list[SheetFailure] = []
    for error in pending_research_errors(
        sheet.pending_research, reserve_usd=sheet.funding.reserve_usd,
        action_symbols={line.symbol for line in sheet.lines}, as_of=sheet.generated_at.date(),
    ):
        failures.append(SheetFailure(code="pending_research_contract", detail=error))
    if sheet.pending_research and (
        sheet.review_resolution is None or not sheet.review_resolution.separation_reviewed
    ):
        failures.append(SheetFailure(
            code="research_separation_unreviewed",
            detail="Every independent reviewer must affirm the revised core/reserve separation.",
        ))
    if sheet.review_resolution is not None and not sheet.review_resolution.one_voice:
        failures.append(
            SheetFailure(
                code="team_review_unresolved",
                detail="material reviewer disagreement remains unresolved",
            )
        )
    symbols: set[str] = set()
    discovery_symbols: set[str] = set()
    buy_total = 0.0
    gross_sells = 0.0
    net_sells = 0.0
    tax_total = 0.0

    for line in sheet.lines:
        if line.symbol in symbols:
            failures.append(
                SheetFailure(
                    code="duplicate_symbol",
                    detail=f"{line.symbol}: more than one action in the one-list sheet",
                )
            )
        symbols.add(line.symbol)
        if line.stance_source == "discovery":
            discovery_symbols.add(line.symbol)

        voice_failure = _voice_failure(line.symbol, line.voices)
        if voice_failure:
            failures.append(voice_failure)

        calculated = line.shares * line.evidence.price_usd
        tolerance = max(1.0, line.notional_usd * 0.0025)
        if not math.isclose(calculated, line.notional_usd, abs_tol=tolerance):
            failures.append(
                SheetFailure(
                    code="quantity_notional_mismatch",
                    detail=(
                        f"{line.symbol}: shares x price = {calculated:.2f}, "
                        f"notional = {line.notional_usd:.2f}"
                    ),
                )
            )

        increments = line.shares / line.quantity_increment
        if not math.isclose(increments, round(increments), abs_tol=1e-8):
            failures.append(
                SheetFailure(
                    code="invalid_quantity_increment",
                    detail=(
                        f"{line.symbol}: {line.shares:g} shares is not a multiple "
                        f"of the venue increment {line.quantity_increment:g}"
                    ),
                )
            )

        age = _age_days(sheet.generated_at, line.evidence.price_as_of)
        if age < -0.01 or age > sheet.freshness_days:
            failures.append(
                SheetFailure(
                    code="stale_price",
                    detail=f"{line.symbol}: price age {age:.2f}d exceeds {sheet.freshness_days}d",
                )
            )

        if line.catalyst.due_date < sheet.generated_at.date():
            failures.append(
                SheetFailure(
                    code="catalyst_not_future_dated",
                    detail=f"{line.symbol}: catalyst date {line.catalyst.due_date} is past",
                )
            )
        if line.expectation.due_date < sheet.generated_at.date():
            failures.append(
                SheetFailure(
                    code="expectation_not_future_dated",
                    detail=f"{line.symbol}: outcome due date is past",
                )
            )

        if line.post_trade_weight_pct < 1.0:
            if line.thesis_type is not ThesisType.CONVEXITY:
                failures.append(
                    SheetFailure(
                        code="small_position_without_convexity",
                        detail=(
                            f"{line.symbol}: {line.post_trade_weight_pct:.2f}% slot "
                            "requires a convexity thesis"
                        ),
                    )
                )

        if line.thesis_type is ThesisType.CONVEXITY:
            scenarios = line.outcome_scenarios
            if len(scenarios) < 3:
                failures.append(
                    SheetFailure(
                        code="convexity_probabilities_missing",
                        detail=f"{line.symbol}: convexity sizing requires at least three terminal scenarios",
                    )
                )
            else:
                probability_total = sum(s.probability_pct for s in scenarios)
                if not math.isclose(probability_total, 100.0, abs_tol=0.05):
                    failures.append(
                        SheetFailure(
                            code="scenario_probability_mismatch",
                            detail=f"{line.symbol}: scenario probabilities sum to {probability_total:.2f}%, not 100%",
                        )
                    )
                if not any(s.terminal_multiple <= 0.2 for s in scenarios):
                    failures.append(
                        SheetFailure(
                            code="wipeout_scenario_missing",
                            detail=f"{line.symbol}: probability model omits an economic-wipeout case (<=0.2x)",
                        )
                    )
                terminal_dates = {s.terminal_date for s in scenarios if s.terminal_date}
                if len(terminal_dates) != 1 or any(s.terminal_date is None for s in scenarios):
                    failures.append(
                        SheetFailure(
                            code="scenario_terminal_date_missing",
                            detail=f"{line.symbol}: every scenario must use one exact terminal date",
                        )
                    )
                else:
                    terminal_date = next(iter(terminal_dates))
                    horizon_days = (terminal_date - sheet.generated_at.date()).days
                    if horizon_days <= 0 or horizon_days > int(sheet.horizon_years_max * 365.25) + 2:
                        failures.append(
                            SheetFailure(
                                code="scenario_terminal_date_out_of_horizon",
                                detail=(
                                    f"{line.symbol}: terminal date {terminal_date} is outside "
                                    f"the requested {sheet.horizon_years_max}-year horizon"
                                ),
                            )
                        )
                authored_upside = line.expected_upside_multiple or 0.0
                if authored_upside and max(s.terminal_multiple for s in scenarios) < authored_upside:
                    failures.append(
                        SheetFailure(
                            code="upside_scenario_missing",
                            detail=f"{line.symbol}: no scenario reaches the authored {authored_upside:g}x case",
                        )
                    )
                weighted = sum(
                    s.probability_pct * s.terminal_multiple for s in scenarios
                ) / 100.0
                if line.probability_weighted_multiple is None or not math.isclose(
                    line.probability_weighted_multiple, weighted, abs_tol=0.001
                ):
                    failures.append(
                        SheetFailure(
                            code="probability_weighted_math_mismatch",
                            detail=f"{line.symbol}: stored probability-weighted multiple does not equal {weighted:.3f}x",
                        )
                    )
            if line.probability_confidence is None or not (line.probability_basis or "").strip():
                failures.append(
                    SheetFailure(
                        code="probability_basis_missing",
                        detail=f"{line.symbol}: probability confidence and evidence/base-rate basis are required",
                    )
                )
            if (line.expected_upside_multiple or 0.0) < 5.0:
                failures.append(
                    SheetFailure(
                        code="small_position_insufficient_asymmetry",
                        detail=(
                            f"{line.symbol}: sub-1% slot requires at least a "
                            "5x authored upside case"
                        ),
                    )
                )

        if line.stance_source == "discovery":
            ev = line.evidence
            missing = []
            if ev.market_cap_usd is None:
                missing.append("market_cap")
            if not ev.incorporation_country:
                missing.append("incorporation_country")
            if ev.market_cap_as_of is None or not ev.market_cap_source:
                missing.append("market_cap_freshness/source")
            if ev.incorporation_as_of is None or not ev.incorporation_source:
                missing.append("incorporation_freshness/source")
            if missing:
                failures.append(
                    SheetFailure(
                        code="discovery_facts_missing",
                        detail=f"{line.symbol}: missing {', '.join(missing)}",
                    )
                )
            else:
                for label, observed in (
                    ("market_cap", ev.market_cap_as_of),
                    ("incorporation", ev.incorporation_as_of),
                ):
                    assert observed is not None
                    fact_age = _age_days(sheet.generated_at, observed)
                    if fact_age < -0.01 or fact_age > sheet.freshness_days:
                        failures.append(
                            SheetFailure(
                                code=f"stale_{label}",
                                detail=(
                                    f"{line.symbol}: {label} age {fact_age:.2f}d "
                                    f"exceeds {sheet.freshness_days}d"
                                ),
                            )
                        )

        if line.estate_situs == "US" and line.action in (
            OrderAction.BUY,
            OrderAction.ADD,
        ):
            priced = [
                c
                for c in line.constraint_costs
                if c.constraint == "us_estate_tax_exposure"
                and c.valuation_basis == "estimated_fmv_at_event"
            ]
            if not priced:
                failures.append(
                    SheetFailure(
                        code="unpriced_us_situs_constraint",
                        detail=(
                            f"{line.symbol}: US-situs exposure needs an estimated "
                            "FMV-at-event cost, not a boolean gate"
                        ),
                    )
                )

        if line.action in (OrderAction.BUY, OrderAction.ADD):
            if line.tax is not None:
                failures.append(
                    SheetFailure(
                        code="buy_has_sale_tax",
                        detail=f"{line.symbol}: buy/add line must not carry sale tax",
                    )
                )
            buy_total += line.notional_usd
        else:
            gross_sells += line.notional_usd
            tax = line.tax
            if tax is None:
                failures.append(
                    SheetFailure(
                        code="sell_missing_after_tax_math",
                        detail=f"{line.symbol}: sell/trim requires TaxImpact",
                    )
                )
                continue
            if not tax.authoritative:
                failures.append(
                    SheetFailure(
                        code="sell_tax_not_authoritative",
                        detail=(
                            f"{line.symbol}: degraded/estimated tax cannot fund "
                            "an actionable order sheet"
                        ),
                    )
                )
            expected_net = tax.gross_proceeds_usd - tax.estimated_tax_usd
            if not math.isclose(tax.gross_proceeds_usd, line.notional_usd, abs_tol=1.0):
                failures.append(
                    SheetFailure(
                        code="sell_gross_mismatch",
                        detail=f"{line.symbol}: tax gross proceeds differ from order notional",
                    )
                )
            if tax.calculation_basis == "capital_gain":
                if tax.cost_basis_usd is None or tax.taxable_gain_usd is None:
                    failures.append(
                        SheetFailure(
                            code="capital_gain_inputs_missing",
                            detail=f"{line.symbol}: capital-gain tax requires basis and gain",
                        )
                    )
                else:
                    expected_gain = max(
                        0.0, tax.gross_proceeds_usd - tax.cost_basis_usd
                    )
                    expected_tax = expected_gain * tax.effective_tax_rate
                    if not math.isclose(
                        tax.taxable_gain_usd, expected_gain, abs_tol=1.0
                    ):
                        failures.append(
                            SheetFailure(
                                code="taxable_gain_mismatch",
                                detail=(
                                    f"{line.symbol}: taxable gain is not gross less basis"
                                ),
                            )
                        )
                    if not math.isclose(
                        tax.estimated_tax_usd, expected_tax, abs_tol=1.0
                    ):
                        failures.append(
                            SheetFailure(
                                code="tax_amount_mismatch",
                                detail=(
                                    f"{line.symbol}: estimated tax does not match "
                                    "gain x rate"
                                ),
                            )
                        )
            else:
                evidence_fields = (
                    tax.evidence_as_of,
                    tax.evidence_age_days,
                    tax.evidence_max_age_days,
                    tax.evidence_expires_on,
                )
                if any(value is None for value in evidence_fields):
                    failures.append(
                        SheetFailure(
                            code="trusted_tax_evidence_missing",
                            detail=f"{line.symbol}: trusted tax result lacks dated freshness evidence",
                        )
                    )
                elif (
                    tax.evidence_age_days > tax.evidence_max_age_days
                    or sheet.generated_at.date() > tax.evidence_expires_on
                ):
                    failures.append(
                        SheetFailure(
                            code="trusted_tax_evidence_stale",
                            detail=(
                                f"{line.symbol}: tax evidence expired "
                                f"{tax.evidence_expires_on}"
                            ),
                        )
                    )
                components = (tax.capital_income_usd, tax.ordinary_income_usd)
                if tax.cost_basis_usd is None or tax.taxable_gain_usd is None or any(
                    value is None for value in components
                ):
                    failures.append(
                        SheetFailure(
                            code="trusted_tax_components_missing",
                            detail=f"{line.symbol}: trusted tax result lacks basis/income components",
                        )
                    )
                elif not math.isclose(
                    tax.taxable_gain_usd,
                    float(tax.capital_income_usd) + float(tax.ordinary_income_usd),
                    abs_tol=1.0,
                ):
                    failures.append(
                        SheetFailure(
                            code="trusted_tax_components_mismatch",
                            detail=f"{line.symbol}: taxable income does not equal capital plus ordinary income",
                        )
                    )
            if not math.isclose(tax.net_proceeds_usd, expected_net, abs_tol=1.0):
                failures.append(
                    SheetFailure(
                        code="net_proceeds_mismatch",
                        detail=f"{line.symbol}: net proceeds do not match after-tax proceeds",
                    )
                )
            tax_total += tax.estimated_tax_usd
            net_sells += tax.net_proceeds_usd

    comparison_tickers = [row.ticker for row in sheet.candidate_comparisons]
    if len(comparison_tickers) != len(set(comparison_tickers)):
        failures.append(
            SheetFailure(
                code="duplicate_candidate_comparison",
                detail="candidate comparison contains duplicate finalists",
            )
        )
    selected_candidates = {
        row.ticker
        for row in sheet.candidate_comparisons
        if row.selection == "SELECTED"
    }
    missing_selected_reason = sorted(discovery_symbols - selected_candidates)
    if missing_selected_reason:
        failures.append(
            SheetFailure(
                code="candidate_comparison_missing",
                detail=(
                    "discovery buys lack persisted winner/loser adjudication: "
                    f"{missing_selected_reason}"
                ),
            )
        )
    from argosy.services.allocation_research import _is_fund
    fund_comparisons = {row.ticker for row in sheet.candidate_comparisons if _is_fund(row.ticker)}
    fund_buy_symbols = {line.symbol for line in sheet.lines
        if line.symbol in fund_comparisons and line.action in (OrderAction.BUY, OrderAction.ADD)}
    orphan_selected = sorted(selected_candidates - discovery_symbols - fund_buy_symbols)
    if orphan_selected:
        failures.append(
            SheetFailure(
                code="candidate_selection_orphaned",
                detail=f"selected finalists are absent from discovery orders: {orphan_selected}",
            )
        )
    discovery_amounts = {
        line.symbol: line.authored_notional_usd or line.notional_usd
        for line in sheet.lines
        if line.stance_source == "discovery"
        and line.action in (OrderAction.BUY, OrderAction.ADD)
    }
    for comparison in sheet.candidate_comparisons:
        if comparison.ticker in fund_comparisons:
            amount = sum((line.authored_notional_usd or line.notional_usd) for line in sheet.lines
                if line.symbol == comparison.ticker and line.action in (OrderAction.BUY, OrderAction.ADD))
            evidence_age = _age_days(sheet.generated_at, comparison.evidence_fresh_as_of)
            if (comparison.research_verdict not in {"HOLD", "TRIM", "SELL"}
                    or comparison.research_conviction is None
                    or not 0 <= evidence_age <= sheet.freshness_days):
                failures.append(SheetFailure(code="fund_comparison_evidence_missing",
                    detail=f"{comparison.ticker}: fund comparison needs a fresh supported fund review"))
            if (comparison.selection != ("SELECTED" if amount else "NOT_SELECTED")
                    or comparison.recommended_position_usd is None
                    or abs(comparison.recommended_position_usd - amount) > 1):
                failures.append(SheetFailure(code="candidate_sizing_order_mismatch",
                    detail=f"{comparison.ticker}: fund disposition must match the actual funded line"))
            continue  # Vehicle comparison; normal order-line safety still applies.
        scenarios = comparison.outcome_scenarios
        if len(scenarios) < 3:
            failures.append(
                SheetFailure(
                    code="candidate_probabilities_missing",
                    detail=f"{comparison.ticker}: finalist lacks equal-basis probability scenarios",
                )
            )
        else:
            probability_total = sum(s.probability_pct for s in scenarios)
            if not math.isclose(probability_total, 100.0, abs_tol=0.05):
                failures.append(
                    SheetFailure(
                        code="candidate_probability_mismatch",
                        detail=f"{comparison.ticker}: finalist probabilities sum to {probability_total:.2f}%",
                    )
                )
            if not any(s.terminal_multiple <= 0.2 for s in scenarios):
                failures.append(
                    SheetFailure(
                        code="candidate_wipeout_missing",
                        detail=f"{comparison.ticker}: finalist omits an economic-wipeout case",
                    )
                )
            terminal_dates = {s.terminal_date for s in scenarios if s.terminal_date}
            if len(terminal_dates) != 1 or any(s.terminal_date is None for s in scenarios):
                failures.append(
                    SheetFailure(
                        code="candidate_terminal_date_missing",
                        detail=f"{comparison.ticker}: finalist scenarios need one exact terminal date",
                    )
                )
            else:
                terminal_date = next(iter(terminal_dates))
                horizon_days = (terminal_date - sheet.generated_at.date()).days
                if horizon_days <= 0 or horizon_days > int(sheet.horizon_years_max * 365.25) + 2:
                    failures.append(
                        SheetFailure(
                            code="candidate_terminal_date_out_of_horizon",
                            detail=(
                                f"{comparison.ticker}: terminal date {terminal_date} is outside "
                                f"the requested {sheet.horizon_years_max}-year horizon"
                            ),
                        )
                    )
        if comparison.probability_confidence is None or not (
            comparison.probability_basis or ""
        ).strip():
            failures.append(
                SheetFailure(
                    code="candidate_probability_basis_missing",
                    detail=f"{comparison.ticker}: probability confidence/basis missing",
                )
            )
        sizing_errors = comparison.sizing_structure_errors()
        if sizing_errors:
            failures.append(
                SheetFailure(
                    code="candidate_sizing_missing",
                    detail=f"{comparison.ticker}: " + "; ".join(sizing_errors),
                )
            )
        elif comparison.selection == "SELECTED" and not math.isclose(
            comparison.recommended_position_usd,
            discovery_amounts.get(comparison.ticker, 0.0),
            abs_tol=1.0,
        ):
            failures.append(
                SheetFailure(
                    code="candidate_sizing_order_mismatch",
                    detail=f"{comparison.ticker}: finalist sizing does not match its order",
                )
            )

    for untouched in sheet.no_action:
        if untouched.symbol in symbols:
            failures.append(
                SheetFailure(
                    code="action_and_no_action",
                    detail=f"{untouched.symbol}: appears in both action and NO-ACTION",
                )
            )
        voice_failure = _voice_failure(untouched.symbol, untouched.voices)
        if voice_failure:
            failures.append(voice_failure)
        non_hold = sorted(
            {
                v.verdict
                for v in untouched.voices
                if v.decision_scope == "current_run" and v.verdict != "HOLD"
            }
        )
        if non_hold:
            failures.append(
                SheetFailure(
                    code="no_action_contradicts_voice",
                    detail=(f"{untouched.symbol}: NO-ACTION cannot hide {non_hold} verdict(s)"),
                )
            )
        if any(v.source == "coverage_missing" for v in untouched.voices):
            failures.append(
                SheetFailure(
                    code="portfolio_review_missing",
                    detail=f"{untouched.symbol}: no current review/stance exists",
                )
            )

    covered = symbols | {row.symbol for row in sheet.no_action}
    missing_coverage = sorted(
        {s.strip().upper() for s in sheet.portfolio_symbols if s.strip()} - covered
    )
    if missing_coverage:
        failures.append(
            SheetFailure(
                code="portfolio_coverage_missing",
                detail=f"held symbols absent from action/NO-ACTION: {missing_coverage}",
            )
        )

    funding = sheet.funding
    if not math.isclose(gross_sells, funding.gross_sell_proceeds_usd, abs_tol=1.0):
        failures.append(
            SheetFailure(
                code="funding_gross_sells_mismatch",
                detail="funding gross sells do not equal sell/trim lines",
            )
        )
    if not math.isclose(tax_total, funding.sell_tax_usd, abs_tol=1.0):
        failures.append(
            SheetFailure(
                code="funding_sell_tax_mismatch",
                detail="funding sell tax does not equal line tax",
            )
        )
    expected_available = (
        funding.new_cash_usd
        + net_sells
        - funding.sell_costs_usd
        - funding.reserve_usd
        - funding.rounding_residual_usd
    )
    if not math.isclose(expected_available, funding.available_to_buy_usd, abs_tol=1.0):
        failures.append(
            SheetFailure(
                code="available_funds_mismatch",
                detail=(
                    "available-to-buy is not new cash + net sells - costs - reserve "
                    "- venue rounding residual"
                ),
            )
        )
    if not math.isclose(buy_total, funding.available_to_buy_usd, abs_tol=1.0):
        failures.append(
            SheetFailure(
                code="buy_total_mismatch",
                detail=(
                    f"buy lines total {buy_total:.2f}; available funds are "
                    f"{funding.available_to_buy_usd:.2f}"
                ),
            )
        )

    return SheetValidation(
        valid=not failures,
        failures=failures,
        buy_total_usd=round(buy_total, 2),
        net_sell_proceeds_usd=round(net_sells, 2),
    )


def require_valid_order_sheet(sheet: OrderSheet) -> OrderSheet:
    """Return ``sheet`` or raise with every machine-readable failure."""

    report = validate_order_sheet(sheet)
    if not report.valid:
        details = "; ".join(f"{f.code}: {f.detail}" for f in report.failures)
        raise ValueError(f"invalid order sheet: {details}")
    return sheet


__all__ = [
    "ConstraintCost",
    "DatedCatalyst",
    "ExecutionClip",
    "FundingSummary",
    "MarketEvidence",
    "NoActionLine",
    "OrderAction",
    "OrderLine",
    "OrderSheet",
    "OutcomeExpectation",
    "SheetFailure",
    "SheetValidation",
    "StagedExecutionPlan",
    "TaxImpact",
    "ThesisType",
    "VoiceVerdict",
    "require_valid_order_sheet",
    "validate_order_sheet",
]
