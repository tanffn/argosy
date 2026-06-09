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

# --- Effective WITHDRAWAL-tax curve (T3.4) ---------------------------------
# The age-banded rates above are the STATUTORY effective rates per source
# (pre-60 = 25% equity CGT). The Monte-Carlo retirement engine, however,
# grosses up the FULL net spend it must draw — i.e. it taxes the whole
# withdrawal, not just the realized gain. Applying the 25% statutory CGT to
# the entire draw over-taxes a long-held, partly-deconcentrated, dividend-
# yielding book (codex MC review 2026-06-04: it inflated the effective draw
# ~33%). Only the realized-GAIN fraction of each sale is taxable; basis,
# accrued cash and return-of-capital are not. ``TAXABLE_GAIN_FRACTION`` is the
# canonical single source for that blend (mirrors what scenario_mc + the
# deterministic cashflow path already assumed); 0.6 is a deliberately
# conservative (errs-toward-more-tax) figure, documented + tunable.
ISRAELI_CGT_RATE: float = 0.25
TAXABLE_GAIN_FRACTION: float = 0.6
# Pre-pension drawdown is taxable-brokerage CGT on the gain fraction.
PRE_67_EFFECTIVE_WITHDRAWAL_RATE: float = ISRAELI_CGT_RATE * TAXABLE_GAIN_FRACTION  # 0.15
# Post-67 the draw is dominated by the pension annuity under rights-fixation;
# the 12% effective band already blends that (it is NOT a CGT-on-gain rate, so
# the gain fraction does not apply).
POST_67_EFFECTIVE_WITHDRAWAL_RATE: float = POST_67_RATE  # 0.12

_MIN_RATE: float = 0.0
_MAX_RATE: float = 1.0
_MIN_AGE: float = 0.0
_MAX_AGE: float = 120.0

# --- Surtax (mas yesef / "additional tax") (T5.7) --------------------------
# Israel levies an annual surtax on the portion of taxable income above a high
# threshold. Per ITA: a 3% base surtax on income above the threshold, and from
# 2025 an additional 2% on CAPITAL/passive income (capital gains, dividends,
# interest, rent) above the same threshold — so capital income carries 5% above
# the threshold while ordinary income carries 3%. The threshold is nominally
# ~₪721,560/yr (2024/2025; frozen, not indexed up in the 2025 budget). These
# are documented, sourced, intake-overridable constants — NOT hidden magic.
# For a retirement DRAWDOWN (~₪280k/yr central spend) the surtax is zero (well
# below the threshold); it bites on large one-off events — RSU vests, a big NVDA
# deconcentration sale — which is exactly where it must be captured.
SURTAX_THRESHOLD_ANNUAL_NIS: float = 721_560.0
SURTAX_RATE_ORDINARY: float = 0.03
SURTAX_RATE_CAPITAL: float = 0.05  # 3% base + 2% capital surcharge (2025+)


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


def effective_withdrawal_tax_at_age(
    age: float,
    *,
    override_flat: float | None = None,
) -> float:
    """Effective tax on a PORTFOLIO WITHDRAWAL at ``age`` years (T3.4).

    This is the single source the Monte-Carlo retirement engine consults to
    gross up the net spend it must draw, replacing the retired flat-10%
    ``withdrawal_tax`` shortcut. Unlike ``effective_tax_rate_at_age`` (which
    returns the per-source statutory band), this returns the rate applied to
    the WHOLE withdrawal:

        age <  67   -> ISRAELI_CGT_RATE × TAXABLE_GAIN_FRACTION   (= 0.15)
        age >= 67   -> POST_67_RATE                               (= 0.12)

    Pre-67 the draw is taxable-brokerage CGT on the realized-gain fraction;
    post-67 it is the pension rights-fixation effective rate. ``override_flat``
    short-circuits the curve (legacy ``apply_age_aware_tax=False`` behavior).
    """
    if override_flat is not None:
        return max(_MIN_RATE, min(_MAX_RATE, float(override_flat)))
    a = max(_MIN_AGE, min(_MAX_AGE, float(age)))
    if a < ANNUITY_AGE:
        return PRE_67_EFFECTIVE_WITHDRAWAL_RATE
    return POST_67_EFFECTIVE_WITHDRAWAL_RATE


def annual_surtax(
    annual_income_nis: float,
    *,
    is_capital: bool = False,
    threshold_nis: float | None = None,
) -> float:
    """Israeli surtax (mas yesef) on the portion of ANNUAL income above the
    threshold (T5.7).

    Capital/passive income (capital gains, dividends, interest, rent) carries
    the higher 5% rate (3% base + 2% capital surcharge, 2025+); ordinary income
    (salary, RSU vest, pension) carries 3%. ``threshold_nis`` overrides the
    default (intake). Income at/below the threshold returns 0; this is the
    SINGLE source of the surtax math for the calculator + any large-event tax.
    """
    thr = SURTAX_THRESHOLD_ANNUAL_NIS if threshold_nis is None else float(threshold_nis)
    excess = max(0.0, float(annual_income_nis) - thr)
    rate = SURTAX_RATE_CAPITAL if is_capital else SURTAX_RATE_ORDINARY
    return excess * rate


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
