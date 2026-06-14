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

import math
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
    # Large-cap GROWTH tilt is its own ~0.21 class (higher beta + mega-cap
    # concentration than the cap-weighted market) — MUST precede the generic
    # equity needles so a growth sleeve is not modeled at the 0.18 market sigma.
    ("growth", "us_growth_equity"),
    ("core equity", "us_equity"),
    ("us equity", "us_equity"),
    ("us-equity", "us_equity"),
    ("equity", "us_equity"),
    # Alternatives (gold/BTC) sleeve — σ≈0.268 at the 80/20 split. These MUST
    # precede the generic ``("alternative", "us_equity")`` fallback below:
    # the canonical sleeve label is "Alternatives (gold/BTC)", which contains
    # the "alternative" substring, so without these the whole sleeve would be
    # mis-classified as 0.18 diversified equity — silently understating the
    # BTC tail and breaking the FI sigma-solver's anchor.
    ("gold", "alternatives"),
    ("bitcoin", "alternatives"),
    ("btc", "alternatives"),
    ("alternatives", "alternatives"),
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


# --- Cross-class correlation model (covariance-aware portfolio sigma) --------
# Portfolio sigma is σ_p = sqrt(wᵀ Σ w) with Σ_ij = ρ_ij·σ_i·σ_j. The plain
# weighted-average blend (Σ wᵢσᵢ) is the ρ=1 special case — perfect correlation,
# the conservative UPPER bound. Real strategic asset classes are not perfectly
# correlated, so the linear blend over-states portfolio volatility; used as the
# fixed-income SIZING rule it over-reserves the defensive sleeve. These tiers are
# documented long-run strategic correlations an adversarial reviewer can
# reconcile (sources: US/non-US equity correlations have risen to ~0.8; stock-
# bond correlation is regime-dependent and modeled at a mildly-positive +0.10
# rather than claiming a negative-correlation hedge):
#   equity↔equity 0.80 | NVDA↔equity 0.65 | equity↔alternatives 0.25
#   equity↔FI 0.10 | alternatives↔FI 0.10 | bonds↔cash 0.40 | same class 1.00
# An UNKNOWN class pair falls back to ρ=1.0 (the conservative linear bound), never
# a silent mid value, so a novel exposure cannot understate risk; the allocation
# layer additionally gates that every class it uses is explicitly modeled.
_EQUITY_CORR_CLASSES = frozenset(
    {"us_equity", "us_growth_equity", "low_vol_equity", "intl_equity",
     "emerging_equity", "real_estate"}
)
_FI_CORR_CLASSES = frozenset({"bonds", "cash"})
KNOWN_CORR_CLASSES = (
    _EQUITY_CORR_CLASSES | _FI_CORR_CLASSES | {"concentrated_equity", "alternatives"}
)


def class_correlation(a: str, b: str) -> float:
    """Pairwise long-run correlation between two sigma-classes (tier table above).
    Unknown classes default to ρ=1.0 — the conservative (linear) bound."""
    if a == b:
        return 1.0
    pair = {a, b}
    if "concentrated_equity" in pair:
        other = (pair - {"concentrated_equity"}).pop()
        if other in _EQUITY_CORR_CLASSES:
            return 0.65
        if other == "alternatives":
            return 0.20
        if other in _FI_CORR_CLASSES:
            return 0.10
        return 1.0
    if "alternatives" in pair:
        other = (pair - {"alternatives"}).pop()
        if other in _EQUITY_CORR_CLASSES:
            return 0.25
        if other in _FI_CORR_CLASSES:
            return 0.10
        return 1.0
    if a in _EQUITY_CORR_CLASSES and b in _EQUITY_CORR_CLASSES:
        return 0.80
    if pair == {"bonds", "cash"}:
        return 0.40
    if (a in _EQUITY_CORR_CLASSES and b in _FI_CORR_CLASSES) or (
        b in _EQUITY_CORR_CLASSES and a in _FI_CORR_CLASSES
    ):
        return 0.10
    return 1.0


def covariance_sigma(items: list[tuple[str, float, float]]) -> float:
    """Covariance-aware portfolio sigma σ_p = sqrt(wᵀ Σ w) for ``items`` =
    (sigma_class, weight, sigma). Weights need not sum to 1 (normalised here); a
    single positive class returns its own sigma (ρ=1 with itself); an empty book
    returns the diversified default."""
    total = sum(max(0.0, w) for _, w, _ in items)
    if total <= 0:
        return DEFAULT_SIGMA_FLAT
    norm = [(cls, w / total, s) for cls, w, s in items if w > 0]
    var = 0.0
    for ci, wi, si in norm:
        for cj, wj, sj in norm:
            var += wi * wj * si * sj * class_correlation(ci, cj)
    return round(math.sqrt(max(0.0, var)), 4)


def sigma_from_composition(composition_pct_by_class: dict[str, float]) -> float:
    """Covariance-aware holdings-weighted sigma from a glidepath composition dict.
    The plain weighted average it replaced assumed ρ=1 (no diversification credit)
    and over-stated portfolio volatility for a diversified book."""
    items: list[tuple[str, float, float]] = []
    for cls_name, pct in composition_pct_by_class.items():
        if pct <= 0:
            continue
        sigma_class = map_glidepath_class_to_sigma_class(cls_name)
        items.append((sigma_class, pct, _SIGMA_BY_CLASS.get(sigma_class, 0.20)))
    return covariance_sigma(items)


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
