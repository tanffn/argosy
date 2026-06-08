"""Time-varying portfolio sigma for the cashflow projection (Wave 8 v2.3).

The single-sigma model in :mod:`argosy.services.retirement.sigma_calibration`
holds today's NVDA-heavy ~34% volatility constant across the full 50-year
horizon. The user's plan ALREADY commits to deconcentrating NVDA from
~65% → 15% over ~24 months; once that glidepath completes, the steady-
state portfolio is closer to sigma ~= 0.18-0.22 (diversified equity +
bonds). Treating sigma as a constant therefore overstates downside risk
for ~95% of the projection horizon.

Approach:
1. ``sigma_today`` = today's holdings-weighted sigma (calls into the
   existing :func:`calibrate_sigma_from_holdings`).
2. ``sigma_planned`` = the same calibrator's arithmetic re-run against
   the POST-glidepath allocation (i.e. the final point of the allocation
   glidepath service). The plan's eventual asset-class mix is mapped
   back onto the ``_SIGMA_BY_CLASS`` table used by the calibrator.
3. Linear interpolate sigma from ``sigma_today`` → ``sigma_planned``
   across the glidepath's months-to-end-date window.
4. Hold ``sigma_planned`` flat for every month past the glidepath end.

Defensive defaults: when no current plan or no snapshot is available,
the curve flat-lines at the diversified-equity default (0.18) for the
whole horizon. This mirrors the calibrator's fallback when a user has
no portfolio rows yet.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from argosy.services.allocation_glidepath import (
    GlidepathPoint,
    compute_allocation_glidepath,
)
from argosy.services.retirement.sigma_calibration import (
    _SIGMA_BY_CLASS,
    calibrate_sigma_from_holdings,
)


DEFAULT_SIGMA_FLAT = 0.18


# Map glidepath asset-class keys to ``_SIGMA_BY_CLASS`` keys. Order
# matters — more specific keywords MUST appear before more generic ones.
_LABEL_TO_SIGMA_CLASS: tuple[tuple[str, str], ...] = (
    ("nvda", "concentrated_equity"),
    ("nvidia", "concentrated_equity"),
    ("tsla", "concentrated_equity"),
    ("individual stock", "concentrated_equity"),
    ("concentrated", "concentrated_equity"),
    # Min-vol / quality-defensive EQUITY — its own ~0.13 class. MUST precede
    # the generic "equity"/"defensive" needles so a min-vol equity sleeve is
    # not collapsed to plain diversified equity (0.18) or, worse, IG bonds
    # (0.06). Allocation-panel caveat 2.
    ("low-vol", "low_vol_equity"),
    ("low vol", "low_vol_equity"),
    ("low-volatility", "low_vol_equity"),
    ("low volatility", "low_vol_equity"),
    ("min-vol", "low_vol_equity"),
    ("min vol", "low_vol_equity"),
    ("minimum volatility", "low_vol_equity"),
    ("cash", "cash"),
    ("hysa", "cash"),
    ("money market", "cash"),
    ("sgov", "cash"),
    ("t-bill", "cash"),
    ("treasury", "bonds"),
    ("bond", "bonds"),
    ("fixed income", "bonds"),
    ("defensive", "bonds"),
    ("reit", "real_estate"),
    ("real estate", "real_estate"),
    ("emerging", "emerging_equity"),
    ("international", "intl_equity"),
    ("intl", "intl_equity"),
    ("ex-us", "intl_equity"),
    ("dividend", "us_equity"),
    ("growth", "us_equity"),
    ("core equity", "us_equity"),
    ("us equity", "us_equity"),
    ("us-equity", "us_equity"),
    ("equity", "us_equity"),
    ("alternative", "us_equity"),
)


@dataclass(frozen=True)
class SigmaCurve:
    """Time-varying sigma — one entry per month from today to horizon end."""

    series: list[float]
    sigma_today: float
    sigma_planned: float
    months_to_steady_state: int

    def at(self, month: int) -> float:
        """Convenience accessor with flat-extend behaviour past the end."""
        if not self.series:
            return DEFAULT_SIGMA_FLAT
        if month < 0:
            return self.series[0]
        if month >= len(self.series):
            return self.series[-1]
        return self.series[month]


# Caveat 1 — a sleeve that merely NAMES the concentrated ticker it EXCLUDES
# ("Growth-ex-NVDA", "US growth (non-NVDA)", "diversified equity excluding
# NVDA") must not match the ``("nvda", "concentrated_equity")`` rule on the
# substring alone. We strip the exclusion phrase before the needle scan so the
# ticker only survives when it is genuinely the held position.
_TICKER_EXCLUSION_RE = re.compile(
    r"\b(?:ex|non|excl|excluding|without|minus|sans)[-\s]*(?:nvda|nvidia|tsla)\b"
)


def map_glidepath_class_to_sigma_class(class_name: str) -> str:
    """Translate a glidepath asset-class key into a ``_SIGMA_BY_CLASS`` key."""
    key = class_name.strip().lower()
    if not key:
        return "us_equity"
    key = _TICKER_EXCLUSION_RE.sub(" ", key)
    for needle, sigma_class in _LABEL_TO_SIGMA_CLASS:
        if needle in key:
            return sigma_class
    return "us_equity"


def sigma_from_composition(composition_pct_by_class: dict[str, float]) -> float:
    """Compute a holdings-weighted sigma from a glidepath composition dict."""
    total = sum(max(0.0, v) for v in composition_pct_by_class.values())
    if total <= 0:
        return DEFAULT_SIGMA_FLAT
    sigma = 0.0
    for cls_name, pct in composition_pct_by_class.items():
        if pct <= 0:
            continue
        weight = pct / total
        sigma_class = map_glidepath_class_to_sigma_class(cls_name)
        cls_sigma = _SIGMA_BY_CLASS.get(sigma_class, 0.20)
        sigma += weight * cls_sigma
    return round(sigma, 4)


def interpolate_sigma_series(
    *,
    sigma_today: float,
    sigma_planned: float,
    months_to_steady_state: int,
    horizon_months: int,
) -> list[float]:
    """Build a per-month sigma series with linear interpolation + flat-extend."""
    horizon = max(0, int(horizon_months))
    n_points = horizon + 1
    if n_points <= 0:
        return []
    if months_to_steady_state <= 0:
        return [sigma_planned] * n_points
    span = min(months_to_steady_state, horizon)
    out: list[float] = []
    for i in range(n_points):
        if i >= span:
            out.append(sigma_planned)
            continue
        frac = i / span
        out.append(sigma_today + (sigma_planned - sigma_today) * frac)
    return out


def _final_composition(points: list[GlidepathPoint]) -> dict[str, float]:
    """Return the composition dict from the LAST glidepath point."""
    if not points:
        return {}
    return dict(points[-1].composition_pct_by_class)


def _months_between(start: date, end: date) -> int:
    """Whole-month delta between two dates."""
    return (end.year - start.year) * 12 + (end.month - start.month)


def compute_sigma_curve(
    session: Session,
    user_id: str,
    *,
    horizon_months: int,
    today: date | None = None,
) -> SigmaCurve:
    """Build the per-month sigma curve for the cashflow projection."""
    today = today or date.today()
    horizon = max(0, int(horizon_months))

    calibration = calibrate_sigma_from_holdings(
        user_id=user_id, session=session
    )
    sigma_today_value = float(calibration.sigma_annual.value)

    # No snapshot path: calibrator returns 0.18 + empty breakdown.
    if not calibration.breakdown:
        flat = sigma_today_value or DEFAULT_SIGMA_FLAT
        return SigmaCurve(
            series=[flat] * (horizon + 1),
            sigma_today=flat,
            sigma_planned=flat,
            months_to_steady_state=0,
        )

    glidepath = compute_allocation_glidepath(session, user_id, today)
    if glidepath is None or not glidepath.points or glidepath.end_date is None:
        return SigmaCurve(
            series=[sigma_today_value] * (horizon + 1),
            sigma_today=sigma_today_value,
            sigma_planned=sigma_today_value,
            months_to_steady_state=0,
        )

    final_comp = _final_composition(glidepath.points)
    sigma_planned_value = sigma_from_composition(final_comp)

    months_to_end = max(
        0, _months_between(glidepath.today or today, glidepath.end_date)
    )

    series = interpolate_sigma_series(
        sigma_today=sigma_today_value,
        sigma_planned=sigma_planned_value,
        months_to_steady_state=months_to_end,
        horizon_months=horizon,
    )

    return SigmaCurve(
        series=series,
        sigma_today=sigma_today_value,
        sigma_planned=sigma_planned_value,
        months_to_steady_state=months_to_end,
    )
