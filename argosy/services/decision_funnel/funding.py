"""Funding gate (step 8, v0) — is a fleet-approved BUY actually payable?

Runs AFTER Stage 3 approves a BUY and BEFORE the north-star surfacing gate, as
DETERMINISTIC policy code (not model discretion). Classifies the buy:

  * ``cash_funded``      — available cash covers it.
  * ``switch_candidate`` — cash short, but an eligible holding could be trimmed
                           to fund it (a sell-to-fund SWITCH). v0 only FLAGS the
                           candidate + ranks sources; the precise tax-lot
                           net-of-tax/friction math is a separate, FX-basis-
                           sensitive step and is NOT computed here (codex: do not
                           surface a switch on degraded tax data).
  * ``unfundable``       — cash short and nothing eligible to sell.
  * ``amount_unknown``   — the buy can't be sized in USD (share-sized order with
                           no price) — we don't guess.

HONESTY (output-trust doctrine + codex Q1): there is NO settlement-date or
pending-proceeds data in the system. So the cash figure is NOMINAL snapshot cash
and is labelled exactly that — ``cash_basis="nominal_snapshot"``,
``settlement_status="unknown"``. Nothing here claims "settled" or "available-now
settled cash", and time-sensitive funding from future proceeds is not supported.
The real settlement / pending-proceeds model is a prerequisite for production
switch surfacing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from argosy.services.decision_funnel.book import BookHolding

# Tickers that can NEVER fund a buy: NVDA's RSU/sale proceeds settle in ~2-3
# weeks (so it can't fund a buy "now"), and it carries its own managed
# deconcentration glide — opportunistic funding must not borrow from it.
INELIGIBLE_FUNDING_TICKERS = frozenset({"NVDA"})

# A degraded-data marker used wherever a number can't be honestly produced.
UNKNOWN = "unknown"


class FundingOutcome(str, Enum):
    CASH_FUNDED = "cash_funded"
    SWITCH_CANDIDATE = "switch_candidate"
    UNFUNDABLE = "unfundable"
    AMOUNT_UNKNOWN = "amount_unknown"


@dataclass(frozen=True)
class FundingAvailability:
    """What cash we can honestly say is available. Settlement timing is NOT
    modelled, so ``settled_cash_usd`` is unknown and we forbid any caller from
    claiming settlement-awareness."""

    snapshot_cash_usd: float | None
    cash_basis: str = "nominal_snapshot"
    settled_cash_usd: None = None
    can_claim_settlement_aware: bool = False

    @property
    def available_usd(self) -> float | None:
        """The honest spendable figure today: nominal snapshot cash. None when
        we have no snapshot cash at all."""
        return self.snapshot_cash_usd


@dataclass(frozen=True)
class FundingSourceCandidate:
    """One holding evaluated as a potential sell-to-fund source X."""

    ticker: str
    eligible: bool
    weight_pct: float | None
    overweight_pp: float | None  # weight - target, when a target is known
    rank_score: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "eligible": self.eligible,
            "weight_pct": self.weight_pct,
            "overweight_pp": self.overweight_pp,
            "rank_score": self.rank_score,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FundingDecision:
    outcome: FundingOutcome
    buy_amount_usd: float | None
    available_cash_usd: float | None
    shortfall_usd: float | None
    cash_basis: str
    settlement_status: str
    selected_source: str | None
    source_candidates: list[FundingSourceCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "buy_amount_usd": self.buy_amount_usd,
            "available_cash_usd": self.available_cash_usd,
            "shortfall_usd": self.shortfall_usd,
            "cash_basis": self.cash_basis,
            "settlement_status": self.settlement_status,
            "selected_source": self.selected_source,
            "source_candidates": [c.to_dict() for c in self.source_candidates],
            "warnings": self.warnings,
            "reason": self.reason,
        }


# Currency-denominated size units → the proposal size IS the USD notional. A
# share-sized order needs a price we don't carry here, so it's amount_unknown.
_CURRENCY_UNITS = {"usd", "currency", "cash", "dollars", "$"}


def buy_amount_usd(size: float | None, size_units: str | None) -> float | None:
    """USD notional of a BUY when honestly derivable, else None.

    A currency-denominated order's size is its USD amount; a share-denominated
    order needs a price (not carried by the funding layer), so we return None
    rather than guess."""
    if size is None or size <= 0:
        return None
    if (size_units or "").strip().lower() in _CURRENCY_UNITS:
        return float(size)
    return None


def build_availability(snapshot_cash_usd: float | None) -> FundingAvailability:
    return FundingAvailability(snapshot_cash_usd=snapshot_cash_usd)


def rank_funding_sources(
    book: list[BookHolding],
    *,
    target_by_ticker: dict[str, float] | None = None,
    buy_ticker: str | None = None,
) -> list[FundingSourceCandidate]:
    """Rank holdings as sell-to-fund sources.

    Eligibility (codex Q3): exclude NVDA always, the buy's own ticker, and rows
    with no usable weight. Ranking favours the most OVERWEIGHT-vs-target name
    (trimming it also serves the plan); when no per-name target is known the
    candidate is still eligible but ranked low with that limitation stated —
    conviction / thesis-fit / tax-lot inputs are NOT available deterministically
    here, so v0 does not pretend to. The precise pick is finalised by the
    tax-lot switch step (a follow-on), which this list feeds.
    """
    target_by_ticker = {k.upper(): v for k, v in (target_by_ticker or {}).items()}
    buy_tk = (buy_ticker or "").upper()
    out: list[FundingSourceCandidate] = []
    for h in book:
        tk = h.ticker.upper()
        if tk in INELIGIBLE_FUNDING_TICKERS:
            out.append(FundingSourceCandidate(
                tk, False, h.weight_pct, None, -1.0,
                "ineligible: NVDA proceeds settle ~2-3 weeks + own deconcentration glide",
            ))
            continue
        if buy_tk and tk == buy_tk:
            out.append(FundingSourceCandidate(
                tk, False, h.weight_pct, None, -1.0,
                "ineligible: cannot fund a buy by selling the same name",
            ))
            continue
        if h.weight_pct is None or h.weight_pct <= 0:
            out.append(FundingSourceCandidate(
                tk, False, h.weight_pct, None, -1.0, "ineligible: no usable weight",
            ))
            continue
        target = target_by_ticker.get(tk)
        overweight = (h.weight_pct - target) if target is not None else None
        if overweight is not None:
            score = overweight  # most overweight first
            reason = (
                f"overweight {overweight:+.1f}pp vs target — trimming serves the plan"
                if overweight > 0
                else f"at/under target ({overweight:+.1f}pp) — trimming would underweight"
            )
        else:
            score = 0.0
            reason = "eligible, but no per-name target — limited ranking (needs thesis/lot review)"
        out.append(FundingSourceCandidate(tk, True, h.weight_pct, overweight, score, reason))
    out.sort(key=lambda c: (not c.eligible, -c.rank_score, c.ticker))
    return out


def classify_funding(
    *,
    buy_amount: float | None,
    availability: FundingAvailability,
    sources: list[FundingSourceCandidate],
) -> FundingDecision:
    """Deterministically classify a fleet-approved BUY's funding."""
    cash = availability.available_usd
    base_warnings = [
        "cash figure is nominal snapshot cash — settlement timing not modelled",
    ]
    common = dict(
        buy_amount_usd=buy_amount,
        available_cash_usd=cash,
        cash_basis=availability.cash_basis,
        settlement_status=UNKNOWN,
        source_candidates=sources,
    )

    if buy_amount is None:
        return FundingDecision(
            outcome=FundingOutcome.AMOUNT_UNKNOWN, shortfall_usd=None,
            selected_source=None,
            warnings=[*base_warnings, "buy size not USD-denominated — cannot fund-check"],
            reason="buy amount not derivable in USD (share-sized order, no price here)",
            **common,
        )

    if cash is None:
        return FundingDecision(
            outcome=FundingOutcome.AMOUNT_UNKNOWN, shortfall_usd=None,
            selected_source=None,
            warnings=[*base_warnings, "no snapshot cash figure available"],
            reason="no cash figure to check against",
            **common,
        )

    if cash >= buy_amount:
        return FundingDecision(
            outcome=FundingOutcome.CASH_FUNDED, shortfall_usd=0.0,
            selected_source=None, warnings=base_warnings,
            reason=f"nominal cash {cash:.0f} covers the {buy_amount:.0f} buy",
            **common,
        )

    shortfall = buy_amount - cash
    eligible = [c for c in sources if c.eligible]
    if eligible:
        return FundingDecision(
            outcome=FundingOutcome.SWITCH_CANDIDATE, shortfall_usd=shortfall,
            selected_source=eligible[0].ticker,
            warnings=[
                *base_warnings,
                "switch candidate only — net-of-tax/friction not computed (needs FX/lot basis); shadow-only",
            ],
            reason=(
                f"cash short by {shortfall:.0f}; {eligible[0].ticker} is the top "
                f"sell-to-fund candidate ({eligible[0].reason})"
            ),
            **common,
        )

    return FundingDecision(
        outcome=FundingOutcome.UNFUNDABLE, shortfall_usd=shortfall,
        selected_source=None,
        warnings=[*base_warnings, "no eligible sell-to-fund source"],
        reason=f"cash short by {shortfall:.0f} and nothing eligible to sell — hold",
        **common,
    )


__all__ = [
    "INELIGIBLE_FUNDING_TICKERS",
    "FundingAvailability",
    "FundingDecision",
    "FundingOutcome",
    "FundingSourceCandidate",
    "build_availability",
    "buy_amount_usd",
    "classify_funding",
    "rank_funding_sources",
]
