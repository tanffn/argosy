"""Deterministic contractual-RSU net savings (B1).

The plan's headline savings floor (``savings.annual_net_nis``) was the LLM equity-comp
agent's ``known_grants_only.five_year_avg_net_nis`` — which swung 297k→284k→218k→312k
(43%) run-to-run because the model ESTIMATED a quantity that is, for contractual grants
already on file, fully COMPUTABLE. This module computes it deterministically from the
contractual vest calendar × NVDA price × USD/NIS FX × the at-vest retention rate
(RSU vesting is ordinary income at vest), pinned to EXACTLY ``horizon_years`` calendar
years (codex flagged the old "2026-2031" span as 6 years, not 5).

Pure: no DB, no LLM. The resolver feeds it the live inputs and binds the result.
"""
from __future__ import annotations

from datetime import date


def _parse_year(value) -> int | None:
    """Calendar year from a date or 'YYYY-MM-DD' string; None if unparseable."""
    if isinstance(value, date):
        return value.year
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10]).year
        except ValueError:
            return None
    return None


def contractual_rsu_net_by_year(
    quarterly_vests,
    *,
    nvda_price_usd: float,
    usd_nis_fx: float,
    at_vest_retention: float,
    horizon_start_year: int,
    horizon_years: int = 5,
) -> tuple[dict[int, float], float]:
    """Deterministic net-of-tax RSU value by calendar year + the N-year mean.

    For each vest event ``{date, shares}`` whose calendar year falls in the pinned
    window ``[horizon_start_year, horizon_start_year + horizon_years)``:
        net_nis += shares * nvda_price_usd * usd_nis_fx * at_vest_retention
    Vests outside the window, or rows missing a parseable date or numeric shares, are
    skipped. Returns ``({year: net_nis}, mean_over_the_full_window)`` — the mean divides
    by ``horizon_years`` (empty years count as 0), never by the count of non-empty years.
    """
    years = list(range(horizon_start_year, horizon_start_year + horizon_years))
    by_year: dict[int, float] = {y: 0.0 for y in years}
    for v in quarterly_vests or []:
        if not isinstance(v, dict):
            continue
        y = _parse_year(v.get("date"))
        if y is None or y not in by_year:
            continue
        shares = v.get("shares")
        try:
            sh = float(shares)
        except (TypeError, ValueError):
            continue
        by_year[y] += sh * float(nvda_price_usd) * float(usd_nis_fx) * float(at_vest_retention)
    mean = sum(by_year.values()) / horizon_years if horizon_years else 0.0
    return by_year, mean


__all__ = ["contractual_rsu_net_by_year"]
