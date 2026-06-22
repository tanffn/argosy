"""Tests for the funding gate (step 8, v0).

Pure deterministic money logic — no DB. Covers buy-amount derivation, the
honest nominal-cash availability (settlement explicitly unknown), funding-source
ranking (NVDA + same-name excluded, overweight-ranked), and the four
classification outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass

from argosy.services.decision_funnel.funding import (
    FundingOutcome,
    build_availability,
    buy_amount_usd,
    classify_funding,
    rank_funding_sources,
)


@dataclass
class _H:
    ticker: str
    weight_pct: float | None
    asset_type: str = "stock"
    usd_value_k: float = 0.0


# --- buy amount + availability honesty -------------------------------------


def test_buy_amount_currency_units():
    assert buy_amount_usd(5000, "usd") == 5000.0
    assert buy_amount_usd(5000, "currency") == 5000.0


def test_buy_amount_share_units_is_unknown():
    assert buy_amount_usd(10, "shares") is None
    assert buy_amount_usd(None, "usd") is None
    assert buy_amount_usd(0, "usd") is None


def test_availability_never_claims_settlement_aware():
    a = build_availability(12_000.0)
    assert a.available_usd == 12_000.0
    assert a.cash_basis == "nominal_snapshot"
    assert a.settled_cash_usd is None
    assert a.can_claim_settlement_aware is False


# --- funding-source ranking ------------------------------------------------


def test_nvda_is_never_an_eligible_source():
    book = [_H("NVDA", 40.0), _H("CSPX", 30.0)]
    ranked = rank_funding_sources(book)
    nvda = next(c for c in ranked if c.ticker == "NVDA")
    assert nvda.eligible is False
    assert "NVDA" in nvda.reason or "settle" in nvda.reason


def test_buy_ticker_excluded_as_its_own_source():
    book = [_H("CSPX", 30.0)]
    ranked = rank_funding_sources(book, buy_ticker="CSPX")
    assert ranked[0].eligible is False


def test_most_overweight_ranks_first():
    book = [_H("AAA", 20.0), _H("BBB", 12.0)]
    targets = {"AAA": 10.0, "BBB": 10.0}  # AAA +10pp, BBB +2pp
    ranked = rank_funding_sources(book, target_by_ticker=targets)
    eligible = [c for c in ranked if c.eligible]
    assert eligible[0].ticker == "AAA"
    assert eligible[0].overweight_pp == 10.0


def test_no_target_is_eligible_but_low_ranked():
    book = [_H("ZZZ", 8.0)]
    ranked = rank_funding_sources(book)
    assert ranked[0].eligible is True
    assert ranked[0].overweight_pp is None
    assert "no per-name target" in ranked[0].reason


# --- classification --------------------------------------------------------


def test_cash_funded_when_cash_covers():
    a = build_availability(20_000.0)
    d = classify_funding(buy_amount=5_000.0, availability=a, sources=[])
    assert d.outcome == FundingOutcome.CASH_FUNDED
    assert d.shortfall_usd == 0.0
    assert d.settlement_status == "unknown"
    # never claims "settled"
    assert all("settled" not in w.lower() or "not" in w.lower() for w in d.warnings)


def test_switch_candidate_when_short_with_eligible_source():
    a = build_availability(1_000.0)
    sources = rank_funding_sources([_H("AAA", 20.0)], target_by_ticker={"AAA": 10.0})
    d = classify_funding(buy_amount=5_000.0, availability=a, sources=sources)
    assert d.outcome == FundingOutcome.SWITCH_CANDIDATE
    assert d.shortfall_usd == 4_000.0
    assert d.selected_source == "AAA"
    # v0 must not pretend it computed the net switch math
    assert any("net-of-tax" in w for w in d.warnings)


def test_unfundable_when_short_and_nothing_to_sell():
    a = build_availability(1_000.0)
    sources = rank_funding_sources([_H("NVDA", 40.0)])  # only NVDA → ineligible
    d = classify_funding(buy_amount=5_000.0, availability=a, sources=sources)
    assert d.outcome == FundingOutcome.UNFUNDABLE


def test_amount_unknown_when_buy_not_usd_sized():
    a = build_availability(20_000.0)
    d = classify_funding(buy_amount=None, availability=a, sources=[])
    assert d.outcome == FundingOutcome.AMOUNT_UNKNOWN


def test_decision_dict_is_serializable_and_honest():
    a = build_availability(1_000.0)
    sources = rank_funding_sources([_H("AAA", 20.0)], target_by_ticker={"AAA": 10.0})
    d = classify_funding(buy_amount=5_000.0, availability=a, sources=sources).to_dict()
    assert d["cash_basis"] == "nominal_snapshot"
    assert d["settlement_status"] == "unknown"
    assert isinstance(d["source_candidates"], list)
