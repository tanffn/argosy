"""Tests for the sell-to-fund switch simulation (step 8).

Pure money-math — no DB. Covers the strict switch gate (conviction delta), HIFO
lot selection, the CGT/friction math, and the honest-degradation rules (no
price / no friction / unknown holding period).
"""

from __future__ import annotations

from datetime import date

from argosy.services.cash_source_reconciler import FALLBACK_IL_CGT_RATE
from argosy.services.decision_funnel.switch import (
    LotInput,
    SwitchPolicy,
    conviction_score,
    should_switch,
    simulate_switch,
)

AS_OF = date(2026, 6, 23)


# --- the switch gate -------------------------------------------------------


def test_high_buy_low_source_is_allowed():
    ok, reason = should_switch(buy_conviction="HIGH", source_conviction="LOW")
    assert ok is True
    assert "exceeds" in reason


def test_non_high_buy_is_blocked():
    ok, reason = should_switch(buy_conviction="MED", source_conviction="LOW")
    assert ok is False
    assert "HIGH" in reason


def test_unknown_source_conviction_is_blocked():
    # Unknown / ungraded source conviction must NOT be treated as low — block it
    # (don't sell a holding to fund another on an assumption).
    ok, reason = should_switch(buy_conviction="HIGH", source_conviction=None)
    assert ok is False
    assert "unknown" in reason.lower()


def test_high_source_is_blocked():
    # Don't sell a high-conviction holding to fund another.
    ok, reason = should_switch(buy_conviction="HIGH", source_conviction="HIGH")
    assert ok is False
    assert "conviction" in reason.lower()


def test_med_source_blocked_as_too_high_conviction():
    # HIGH buy vs MED source: blocked because MED is too high-conviction to sell
    # (this rule fires before the gap check under the default policy).
    ok, reason = should_switch(buy_conviction="HIGH", source_conviction="MED")
    assert ok is False
    assert "conviction" in reason.lower()


def test_routine_swap_blocked_by_gap_under_strict_policy():
    # With a stricter delta, a HIGH-vs-LOW (gap 2) is a routine swap, not a switch.
    ok, reason = should_switch(
        buy_conviction="HIGH", source_conviction="LOW",
        policy=SwitchPolicy(min_conviction_delta=3.0),
    )
    assert ok is False
    assert "routine" in reason


def test_conviction_score_mapping():
    assert conviction_score("HIGH") == 3.0
    assert conviction_score("MEDIUM") == 2.0
    assert conviction_score("low") == 1.0
    assert conviction_score(None) == 0.0


# --- HIFO lot selection + tax math -----------------------------------------


def _lots():
    # Two lots of the source: a high-basis lot (small gain) + a low-basis lot.
    return [
        LotInput("lot-low", quantity=100, cost_basis_usd=1_000.0, acquired_at=date(2020, 1, 1)),   # $10/sh
        LotInput("lot-high", quantity=100, cost_basis_usd=9_000.0, acquired_at=date(2026, 1, 1)),  # $90/sh
    ]


def test_hifo_picks_highest_basis_first_to_minimize_gain():
    # Price $100, need $5,000 → ~50 shares. HIFO sells the $90/sh lot first
    # (gain $10/sh) rather than the $10/sh lot (gain $90/sh).
    sim = simulate_switch(
        buy_ticker="ASML", shortfall_usd=5_000.0, source_ticker="SPMV",
        lots=_lots(), current_price_usd=100.0, as_of=AS_OF, friction_usd=20.0,
    )
    assert sim.sell is not None
    assert sim.sell.lot_ids == ["lot-high"]  # highest-basis lot used first
    # 50 sh × $100 = $5,000 gross; basis 50×$90 = $4,500; gain $500.
    assert sim.gross_proceeds_usd == 5_000.0
    assert sim.sell.realized_gain_usd == 500.0
    assert sim.estimated_cgt_usd == round(500.0 * FALLBACK_IL_CGT_RATE, 2)


def test_friction_unknown_marks_degraded_and_no_net():
    sim = simulate_switch(
        buy_ticker="ASML", shortfall_usd=5_000.0, source_ticker="SPMV",
        lots=_lots(), current_price_usd=100.0, as_of=AS_OF, friction_usd=None,
    )
    assert sim.degraded is True
    assert sim.net_fundable_usd is None
    assert sim.covers_shortfall is False
    assert any("friction" in r for r in sim.degraded_reasons)


def test_friction_supplied_computes_net_and_coverage():
    sim = simulate_switch(
        buy_ticker="ASML", shortfall_usd=5_000.0, source_ticker="SPMV",
        lots=_lots(), current_price_usd=100.0, as_of=AS_OF, friction_usd=10.0,
    )
    # net = 5000 gross - 125 cgt - 10 friction = 4865 < 5000 → doesn't fully cover
    assert sim.net_fundable_usd == 4_865.0
    assert sim.covers_shortfall is False


def test_switch_is_always_degraded_on_fx_basis():
    # Even with friction + known dates, the CGT estimate lacks NIS basis + FX,
    # so a switch is never a confident, non-degraded recommendation.
    sim = simulate_switch(
        buy_ticker="ASML", shortfall_usd=5_000.0, source_ticker="SPMV",
        lots=_lots(), current_price_usd=100.0, as_of=AS_OF, friction_usd=10.0,
    )
    assert sim.degraded is True
    assert any("NIS" in r or "FX" in r for r in sim.degraded_reasons)


def test_no_price_is_degraded_with_no_sell_leg():
    sim = simulate_switch(
        buy_ticker="ASML", shortfall_usd=5_000.0, source_ticker="SPMV",
        lots=_lots(), current_price_usd=None, as_of=AS_OF, friction_usd=10.0,
    )
    assert sim.degraded is True
    assert sim.sell is None
    assert sim.net_fundable_usd is None


def test_cgt_estimate_always_flagged_as_fx_degraded():
    sim = simulate_switch(
        buy_ticker="ASML", shortfall_usd=1_000.0, source_ticker="SPMV",
        lots=_lots(), current_price_usd=100.0, as_of=AS_OF, friction_usd=5.0,
    )
    assert any("NIS" in w or "FX" in w for w in sim.warnings)


def test_unknown_acquisition_date_marks_holding_unknown():
    lots = [LotInput("x", quantity=100, cost_basis_usd=1000.0, acquired_at=None)]
    sim = simulate_switch(
        buy_ticker="ASML", shortfall_usd=1_000.0, source_ticker="SPMV",
        lots=lots, current_price_usd=100.0, as_of=AS_OF, friction_usd=5.0,
    )
    assert sim.sell.holding_period == "unknown"
    assert any("holding period" in r for r in sim.degraded_reasons)


def test_strict_policy_blocks_more():
    # A tighter policy (delta 3) blocks a HIGH-vs-LOW (gap 2) switch.
    ok, _ = should_switch(
        buy_conviction="HIGH", source_conviction="LOW",
        policy=SwitchPolicy(min_conviction_delta=3.0),
    )
    assert ok is False
