"""Risk-budget sell sizing — the objective-based exception (protect the safe-FI
plan), NOT a price/momentum call.

Breach: current NVDA concentration is high enough that an approved idiosyncratic
NVDA shock would push net worth below the FI floor (the perpetuity base). Size =
the smallest sale so that AFTER the shock, net worth still clears the floor."""
from __future__ import annotations

from argosy.services.nvda_risk_budget import risk_budget_sale_nis


def test_no_breach_returns_zero():
    """If a 40% NVDA drop still leaves NW above the floor, there is no risk-budget
    sell (the routine glide handles pacing)."""
    # W=10M, NVDA=2M, 40% shock => post-shock 9.2M >= floor 8M => no breach.
    s = risk_budget_sale_nis(
        net_worth_nis=10_000_000, nvda_value_nis=2_000_000,
        perpetuity_base_nis=8_000_000, shock=0.40,
    )
    assert s == 0.0


def test_breach_sizes_sale_to_restore_floor_under_shock():
    """When the shock breaches the floor, the sale is the smallest that restores
    post-shock NW exactly to the floor."""
    # W=10M, NVDA=6M, 40% shock => post-shock 7.6M < floor 8M => breach.
    W, V, P, d = 10_000_000.0, 6_000_000.0, 8_000_000.0, 0.40
    s = risk_budget_sale_nis(
        net_worth_nis=W, nvda_value_nis=V, perpetuity_base_nis=P, shock=d,
    )
    assert s > 0
    # Post-shock NW after selling s (tax_drag=0): (W) - d*(V - s) == P.
    post_shock = W - d * (V - s)
    assert abs(post_shock - P) <= 1.0


def test_tax_drag_increases_required_sale():
    """A tax drag on proceeds means a slightly larger gross sale is needed to hit
    the same post-shock floor."""
    args = dict(net_worth_nis=10_000_000.0, nvda_value_nis=6_000_000.0,
                perpetuity_base_nis=8_000_000.0, shock=0.40)
    s_no_tax = risk_budget_sale_nis(**args, tax_drag=0.0)
    s_tax = risk_budget_sale_nis(**args, tax_drag=0.25)
    assert s_tax > s_no_tax


def test_sale_capped_at_full_nvda_position():
    """Never recommend selling more NVDA than is held, even if the floor cannot be
    fully restored by selling NVDA alone."""
    s = risk_budget_sale_nis(
        net_worth_nis=5_000_000.0, nvda_value_nis=1_000_000.0,
        perpetuity_base_nis=9_000_000.0, shock=0.50,
    )
    assert s == 1_000_000.0


def test_tax_drag_at_or_above_shock_cannot_restore_floor_returns_zero():
    """If the effective tax drag meets/exceeds the shock, selling NVDA cannot
    restore the floor — do not recommend a sale that fails the objective."""
    s = risk_budget_sale_nis(
        net_worth_nis=10_000_000.0, nvda_value_nis=6_000_000.0,
        perpetuity_base_nis=8_000_000.0, shock=0.20, tax_drag=0.25,
    )
    assert s == 0.0


def test_missing_inputs_return_zero():
    """No FI floor / net worth resolved → no risk-budget sell (fail-safe: never
    over-sell on missing data)."""
    assert risk_budget_sale_nis(
        net_worth_nis=0.0, nvda_value_nis=6_000_000.0, perpetuity_base_nis=8_000_000.0,
    ) == 0.0
    assert risk_budget_sale_nis(
        net_worth_nis=10_000_000.0, nvda_value_nis=6_000_000.0, perpetuity_base_nis=0.0,
    ) == 0.0
