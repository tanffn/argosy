# argosy/services/fi_crossing.py
"""Deterministic FI-capital crossing year.

The first calendar year the FUTURE VALUE of current liquid net worth plus a
real-savings annuity reaches the FI total-capital target. All inputs are REAL
(the resolver's return + FI target are real), so the projection is real — no
inflation double-count. Reconciled with the FI margin by construction: if liquid
already clears the target the crossing is the current year; otherwise it is
strictly in the future. Pure: no DB, no LLM.
"""
from __future__ import annotations


def _future_value(liquid_now: float, real_return: float,
                  annual_real_savings: float, n: int) -> float:
    """Real future value after ``n`` years with end-of-year (ordinary) savings."""
    if n <= 0:
        return liquid_now
    if real_return == 0.0:
        return liquid_now + annual_real_savings * n
    growth = (1.0 + real_return) ** n
    return liquid_now * growth + annual_real_savings * (growth - 1.0) / real_return


def fi_crossing_year(
    *, liquid_now: float, fi_total: float, real_return: float,
    annual_real_savings: float, current_year: int, horizon_years: int = 60,
) -> int | None:
    """Smallest year >= current_year whose projected REAL net worth >= fi_total.

    ``real_return`` is a DECIMAL FRACTION (0.03 = 3% real); a magnitude >= 1 (or
    <= -1) is almost certainly percent-points passed by mistake -> raise, never
    silently project a 300% return (codex #1). ``annual_real_savings`` is constant
    real (end-of-year). Returns None when the target is not reached within
    ``horizon_years``. Already-at-or-above-target -> current_year."""
    if not (-1.0 < real_return < 1.0):
        raise ValueError(
            f"real_return must be a decimal fraction in (-1, 1), got {real_return!r} "
            "(did you pass percent-points instead of a fraction?)")
    for n in range(0, horizon_years + 1):
        if _future_value(liquid_now, real_return, annual_real_savings, n) >= fi_total:
            return current_year + n
    return None
