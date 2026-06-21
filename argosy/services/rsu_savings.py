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


_QUARTER_MONTHS = (3, 6, 9, 12)


def _first_vest_quarter(award_year: int, award_month: int) -> tuple[int, int]:
    """First quarterly vest month (one of 3/6/9/12) strictly after the award month.
    NVIDIA RSUs vest quarterly with no 1-yr cliff (codex-confirmed against the portal:
    the 2026-03 grant vests in 2026-06)."""
    for m in _QUARTER_MONTHS:
        if m > award_month:
            return award_year, m
    return award_year + 1, _QUARTER_MONTHS[0]


def _add_quarters(year: int, month: int, k: int) -> tuple[int, int]:
    idx = _QUARTER_MONTHS.index(month) + k
    return year + idx // 4, _QUARTER_MONTHS[idx % 4]


def project_quarterly_vests(
    active_grants,
    portal_vests,
    *,
    horizon_start_year: int,
    horizon_years: int = 5,
    vesting_quarters: int = 16,
) -> list[dict]:
    """Deterministic forward vest calendar (B1) — list of ``{date, shares}`` events.

    Each active grant vests ``quarterly_shares`` for ``vesting_quarters`` quarters
    (4 years × 4) from the first quarter after its award, no cliff. The authoritative
    ``portal_vests`` (RSU-portal screenshot) OVERRIDE the projected aggregate for the
    quarters they cover — they capture one-time catch-up tranches (e.g. the +278 in
    2026-06 = the final 2022-grant runoff) that the steady per-grant rate misses.

    Only vests on/after the earliest portal date (the forward "as-of") are emitted, so
    already-realized vests earlier in the start year are excluded. codex-reviewed; the
    per-year buckets reproduce the codex projection (2026:1638 .. 2030:57). Pure."""
    agg: dict[tuple[int, int], float] = {}
    for g in active_grants or []:
        if not isinstance(g, dict):
            continue
        q = g.get("quarterly_shares") or g.get("quarterly_shares_approx") or 0
        try:
            q = float(q)
        except (TypeError, ValueError):
            continue
        if q <= 0:
            continue
        ad_year = _parse_year(g.get("award_date"))
        ad = g.get("award_date")
        ad_month = None
        if isinstance(ad, str):
            try:
                ad_month = date.fromisoformat(ad.strip()[:10]).month
            except ValueError:
                ad_month = None
        elif isinstance(ad, date):
            ad_month = ad.month
        if ad_year is None or ad_month is None:
            continue
        vy, vm = _first_vest_quarter(ad_year, ad_month)
        for k in range(vesting_quarters):
            qy, qm = _add_quarters(vy, vm, k)
            agg[(qy, qm)] = agg.get((qy, qm), 0.0) + q

    # Portal override (authoritative; replaces the projected steady rate for its quarters).
    portal_keys: dict[tuple[int, int], float] = {}
    for v in portal_vests or []:
        if not isinstance(v, dict):
            continue
        d = v.get("date")
        d_year = d_month = None
        if isinstance(d, str):
            # accept 'YYYY-MM' or 'YYYY-MM-DD' (the portal uses both)
            parts = d.strip().split("-")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                d_year, d_month = int(parts[0]), int(parts[1])
        elif isinstance(d, date):
            d_year, d_month = d.year, d.month
        sh = v.get("shares")
        if d_year is None or d_month is None or sh is None:
            continue
        qm = min(_QUARTER_MONTHS, key=lambda mm: abs(mm - d_month))
        portal_keys[(d_year, qm)] = float(sh)
    for k, sh in portal_keys.items():
        agg[k] = sh

    as_of = min(portal_keys) if portal_keys else (horizon_start_year, 6)
    end_year = horizon_start_year + horizon_years
    events: list[dict] = []
    for (qy, qm) in sorted(agg):
        if (qy, qm) < as_of:
            continue
        if not (horizon_start_year <= qy < end_year):
            continue
        sh = agg[(qy, qm)]
        if sh:
            events.append({"date": f"{qy:04d}-{qm:02d}-15", "shares": sh})
    return events


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
