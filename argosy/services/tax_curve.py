"""Unified age-banded effective-tax curve for cashflow projections.

Wave 8 v2.3 deep-audit #2 fix: the deterministic ``project_cashflow`` was
applying a flat ``tax_rate`` slider while the Monte-Carlo path
``project_monte_carlo`` already age-banded the rate inline. The two
engines therefore disagreed on the same projection. This module is the
single source of truth both paths now call so they agree.

Bands (Israeli rules; mirror the inline numbers MC used at
cashflow_projection.py ~L840-853, fuller per-source breakdown lives in
argosy/services/retirement/tax_engine.py):

    age <  60          ->  0.25   pre_60_cgt          (taxable CGT 25%)
    60 <= age <  67    ->  0.15   lump_window_60_67   (hishtalmut tax-free
                                                       lump + gemel blend)
    age >= 67          ->  0.12   post_67_pension     (rights-fixation
                                                       exemption blended)

``override_flat`` short-circuits the curve — used when the caller wants
legacy flat behavior (e.g. ``apply_age_aware_tax=False`` /
``goals_yaml.tax_rate_pct``).
"""
from __future__ import annotations

from dataclasses import dataclass


LUMP_PENSION_AGE: float = 60.0
ANNUITY_AGE: float = 67.0

PRE_60_RATE: float = 0.25
LUMP_WINDOW_RATE: float = 0.15
POST_67_RATE: float = 0.12

_MIN_RATE: float = 0.0
_MAX_RATE: float = 1.0
_MIN_AGE: float = 0.0
_MAX_AGE: float = 120.0


@dataclass(frozen=True)
class TaxCurvePoint:
    age_years: float
    effective_rate: float  # 0-1
    source_band: str  # "pre_60_cgt", "lump_window_60_67", "post_67_pension"


def _band_for_age(age: float) -> tuple[float, str]:
    """Return (rate, band_label). Clamps negatives to pre-60 and >120 to post-67."""
    a = max(_MIN_AGE, min(_MAX_AGE, float(age)))
    if a < LUMP_PENSION_AGE:
        return PRE_60_RATE, "pre_60_cgt"
    if a < ANNUITY_AGE:
        return LUMP_WINDOW_RATE, "lump_window_60_67"
    return POST_67_RATE, "post_67_pension"


def effective_tax_rate_at_age(
    age: float,
    *,
    override_flat: float | None = None,
) -> float:
    """Return the age-banded effective rate at ``age`` years.

    If ``override_flat`` is given, return that (clipped to [0,1]) at every
    age — preserves legacy deterministic-flat behavior.
    """
    if override_flat is not None:
        return max(_MIN_RATE, min(_MAX_RATE, float(override_flat)))
    rate, _ = _band_for_age(age)
    return rate


def build_tax_curve(
    current_age: float,
    horizon_months: int,
    *,
    override_flat: float | None = None,
) -> list[TaxCurvePoint]:
    """Per-month tax curve from current_age out to current_age + horizon_months/12.

    Returns horizon_months + 1 points so callers index by tick t in [0, N].
    """
    n = max(0, int(horizon_months))
    points: list[TaxCurvePoint] = []
    for t in range(n + 1):
        age_t = float(current_age) + t / 12.0
        if override_flat is not None:
            rate = max(_MIN_RATE, min(_MAX_RATE, float(override_flat)))
            band = "override_flat"
        else:
            rate, band = _band_for_age(age_t)
        points.append(TaxCurvePoint(age_t, rate, band))
    return points
