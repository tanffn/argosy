"""Risk-budget sell sizing — the objective-based exception in the NVDA sell
protocol (protect the safe-FI plan), not a price/momentum call.

A risk-budget breach exists when the current NVDA concentration is high enough
that an approved idiosyncratic NVDA shock ``d`` (default 40%) would push net worth
below the FI floor (the perpetuity base ``P``). The sell is sized as the SMALLEST
gross sale that brings post-shock net worth back to the floor — computed backward
from the objective, per the codex methodology, not a fixed "vol up → sell X%".

Derivation (sell ``s`` gross of NVDA, effective tax drag ``e`` on proceeds):
    post-shock NW  =  (W − e·s)  −  d·(V − s)   ≥   P
    ⇒  s · (d − e)  ≥  P − W + d·V
    ⇒  s*  =  (P − W + d·V) / (d − e)
clamped to ``[0, V]`` (never sell more NVDA than held). ``d > e`` in every
realistic case; if not, the floor cannot be restored by selling NVDA alone and we
recommend selling the whole position.
"""
from __future__ import annotations


def risk_budget_sale_nis(
    *,
    net_worth_nis: float,
    nvda_value_nis: float,
    perpetuity_base_nis: float,
    shock: float = 0.40,
    tax_drag: float = 0.0,
) -> float:
    """The smallest NVDA sale (NIS) that keeps net worth above the FI floor under a
    ``shock`` NVDA drawdown. ``0.0`` when there is no breach or inputs are missing
    (fail-safe: never over-sell on missing data)."""
    W, V, P, d, e = (
        float(net_worth_nis), float(nvda_value_nis), float(perpetuity_base_nis),
        float(shock), float(tax_drag),
    )
    if W <= 0 or V <= 0 or P <= 0 or d <= 0:
        return 0.0
    # No breach: even after the shock, net worth clears the floor with no action.
    if (W - d * V) >= P:
        return 0.0
    denom = d - e
    if denom <= 0:
        # Tax drag ≥ shock: selling NVDA cannot improve post-shock net worth, so no
        # NVDA sale restores the floor. Return 0 rather than recommend a sale that
        # fails the objective — floor protection here needs a different lever.
        return 0.0
    s = (P - W + d * V) / denom
    return round(min(max(0.0, s), V), 2)


__all__ = ["risk_budget_sale_nis"]
