"""The structured allocation the FLEET authors — the object determinism verifies.

The author (an LLM agent) reasons holistically over the decision packet and emits
this; it is never produced by deterministic code. Every material instrument claim
the agent relies on (e.g. "FWRA is ex-US") is carried as a field so the verifier can
check it against sourced facts rather than trusting prose.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from argosy.services.order_sheet import CandidateComparison, OutcomeScenario, PendingResearch, ThesisType


class AuthoredOrderIntent(BaseModel):
    """Judgment metadata required to turn an allocation amount into an order.

    Price, shares, venue, taxes and constraint prices are resolved downstream
    from live/portfolio facts.  These fields are the author's judgment and must
    therefore be authored rather than inferred by deterministic code.
    """

    thesis: str = Field(min_length=1)
    thesis_type: ThesisType
    falsifier: str = Field(min_length=1)
    catalyst_description: str = Field(min_length=1)
    catalyst_date: date
    expectation: str = Field(min_length=1)
    expectation_due_date: date
    success_measure: str = Field(min_length=1)
    expected_upside_multiple: float | None = Field(default=None, gt=0)
    # Required for convexity sizing. The author owns these judgment calls; the
    # verifier checks only completeness/sum and downstream code computes EV.
    outcome_scenarios: list[OutcomeScenario] = Field(default_factory=list)
    probability_confidence: Literal["LOW", "MED", "HIGH"] | None = None
    probability_basis: str | None = None
    # Required for moonshot sizing: explicit authored judgment, not magic words
    # parsed from justification prose. Non-moonshot orders may leave it null.
    downside_class: Literal[
        "ASSET_BACKED",
        "EARNING_POWER",
        "FUNDED_OPTIONALITY",
    ] | None = None
    # Acknowledges that a US-situs position carries an FMV-at-death estate cost.
    # The downstream fact layer prices it; the author must only acknowledge it.
    acknowledges_us_situs_estate_cost: bool = False


class Buy(BaseModel):
    symbol: str
    amount_usd: float = Field(ge=0.0)  # a buy can never be negative (schema defense)
    sleeve: str = ""
    justification: str = ""
    # The agent's claim about the instrument's US-equity weight (0..1), checked
    # against the sourced InstrumentFacts registry by the verifier. Required at the
    # gate — a None here makes the look-through cross-check un-skippable.
    claimed_us_weight: float | None = None
    order_intent: AuthoredOrderIntent | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_ticker_alias(cls, value: object) -> object:
        if isinstance(value, dict) and "symbol" not in value and "ticker" in value:
            return {**value, "symbol": value["ticker"]}
        return value


class Sell(BaseModel):
    symbol: str
    amount_usd: float = Field(ge=0.0)
    reason: str = ""
    execution_style: Literal["single", "staged_tranche"] = "single"
    execute_by: date | None = None
    next_review_date: date | None = None
    tranche_reason: str = ""
    order_intent: AuthoredOrderIntent | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_ticker_alias(cls, value: object) -> object:
        if isinstance(value, dict) and "symbol" not in value and "ticker" in value:
            return {**value, "symbol": value["ticker"]}
        return value


class AllocationProposal(BaseModel):
    """The fleet's authored move for a deploy request."""

    # Non-negativity is a schema invariant AND re-checked in the verifier (the
    # authoritative money gate): a negative reserve must never be able to balance an
    # over-deploy through the pure-equality conservation checks.
    #
    # There is deliberately NO tax-reserve field. Capital-gains tax on a sale is paid
    # from that sale's own proceeds when it is realized — you do not pre-fund a future
    # sale's tax out of unrelated deployment cash. The deployable amount is treated as
    # already net-of-tax.
    cash_to_deploy: float = Field(ge=0.0)
    cash_to_reserve: float = Field(default=0.0, ge=0.0)
    buys: list[Buy] = Field(default_factory=list)
    sells: list[Sell] = Field(default_factory=list)
    holds: list[str] = Field(default_factory=list)
    candidate_comparisons: list[CandidateComparison] = Field(default_factory=list)
    pending_research: list[PendingResearch] = Field(default_factory=list)
    research_separation_blocker: str | None = None
    rationale: str = ""


__all__ = [
    "AllocationProposal",
    "AuthoredOrderIntent",
    "Buy",
    "CandidateComparison",
    "Sell",
]
