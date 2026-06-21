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
                  annual_real_savings: float, n: int,
                  *, savings_by_year: dict[int, float] | None = None,
                  current_year: int | None = None) -> float:
    """Real future value after ``n`` years with end-of-year (ordinary) savings.

    Scalar path (``savings_by_year is None``): closed-form ordinary annuity, the
    same arithmetic as before. Vector path: iterate year by year so the saving
    in calendar year ``current_year + i`` is added at the end of year ``i``;
    years absent from the vector fall back to ``annual_real_savings`` (the
    post-window tail floor). The vector path reduces to the closed form when the
    per-year amount is constant, so the two agree on a flat stream (B1)."""
    if n <= 0:
        return liquid_now
    if savings_by_year is not None:
        bal = liquid_now
        for i in range(n):
            contrib = savings_by_year.get(current_year + i, annual_real_savings)
            bal = bal * (1.0 + real_return) + contrib
        return bal
    if real_return == 0.0:
        return liquid_now + annual_real_savings * n
    growth = (1.0 + real_return) ** n
    return liquid_now * growth + annual_real_savings * (growth - 1.0) / real_return


def fi_crossing_year(
    *, liquid_now: float, fi_total: float, real_return: float,
    annual_real_savings: float, current_year: int, horizon_years: int = 60,
    savings_by_year: dict[int, float] | None = None,
) -> int | None:
    """Smallest year >= current_year whose projected REAL net worth >= fi_total.

    ``real_return`` is a DECIMAL FRACTION (0.03 = 3% real); a magnitude >= 1 (or
    <= -1) is almost certainly percent-points passed by mistake -> raise, never
    silently project a 300% return (codex #1). Returns None when the target is not
    reached within ``horizon_years``. Already-at-or-above-target -> current_year.

    ``annual_real_savings`` is the constant real (end-of-year) floor. When
    ``savings_by_year`` is supplied (B1), the saving for each calendar year is
    taken from it and years absent from it fall back to ``annual_real_savings``
    as the post-window tail — so the contractual RSU vector (front-loaded, runs
    off by ~2030) drives the in-window years while the conservative floor governs
    after. The flat-scalar path is unchanged when no vector is passed."""
    if not (-1.0 < real_return < 1.0):
        raise ValueError(
            f"real_return must be a decimal fraction in (-1, 1), got {real_return!r} "
            "(did you pass percent-points instead of a fraction?)")
    for n in range(0, horizon_years + 1):
        if _future_value(liquid_now, real_return, annual_real_savings, n,
                          savings_by_year=savings_by_year,
                          current_year=current_year) >= fi_total:
            return current_year + n
    return None
