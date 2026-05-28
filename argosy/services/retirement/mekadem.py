"""Mekadem (annuity coefficient) variance band per Israeli pension fund.

Annuity math: ``monthly_annuity_nis = pension_balance_nis / mekadem``.
A higher mekadem → lower monthly annuity; a lower mekadem → higher annuity.

The plan-document gap (BLOCKER #3) was that the projection used a single
hardcoded mekadem=200. Real mekadem varies by:
  - Fund (Clal ≈ 200, Migdal ≈ 198, Menorah ≈ 202 per published 2026 tables)
  - Mortality table the fund applies (newer tables → slightly lower mekadem)
  - Spouse benefit selection (with-spouse path uses a higher mekadem)
  - Rights-fixation date (impacts which actuarial regime applies)

This module surfaces a band ``(low, typical, high)`` around the user's
fund-specific typical. The band width is a heuristic ±2.5% covering the
"we don't know your exact policy details" envelope. The UI shows the band
on the cashflow chart's annuity line and surfaces the rationale via tooltips.

Plan: `docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md` § Wave 1.
"""
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.retirement.reference import ResolveError, resolve

# Heuristic band width in fraction units. ±2.5% reflects:
#   - Spouse-benefit toggle: ~1.5%
#   - Mortality-table generation: ~1%
# Conservative envelope; user-supplied per-policy values override entirely.
_BAND_WIDTH = 0.025


SupportedFund = Literal["clal_pensia", "migdal_pensia", "menorah_pensia"]


@dataclass(frozen=True)
class MekademBand:
    """Per-fund mekadem band (low / typical / high).

    ``low`` is favorable to the user (lower mekadem → higher annuity).
    ``high`` is unfavorable (higher mekadem → lower annuity).
    ``typical`` is the published central estimate.

    All three values are ``ValueWithRationale`` so the UI can hover-explain
    each separately. The ``typical`` value carries the canonical source_id;
    ``low`` and ``high`` are derived (source_id=None) with a rationale
    pointing back to the heuristic + the typical's source.
    """
    fund_id: str
    typical: ValueWithRationale
    low: ValueWithRationale
    high: ValueWithRationale


def get_mekadem_for_fund(
    fund_id: SupportedFund,
    *,
    user_id: str,
    session: Session,
    band_width: float = _BAND_WIDTH,
) -> MekademBand:
    """Resolve the mekadem band for the user's pension fund.

    Looks up ``mekadem.<fund_id>`` from the hybrid-defaults resolver. The
    user-override path takes precedence: if the user has supplied an
    exact mekadem via intake, ``typical`` is that value (and ``low`` /
    ``high`` are still heuristic-derived from it; user can also override
    the band width by supplying ``mekadem.<fund_id>_band_width``).

    Raises ``ValueError`` if ``fund_id`` is unknown.
    """
    try:
        typical_vwr = resolve(
            f"mekadem.{fund_id}", user_id=user_id, session=session,
        )
    except ResolveError as e:
        raise ValueError(
            f"unsupported fund_id={fund_id!r}; resolver returned {e}"
        ) from e

    if not isinstance(typical_vwr.value, (int, float)):
        raise ValueError(
            f"mekadem value for {fund_id!r} is not numeric: {typical_vwr.value!r}"
        )
    typical_value = float(typical_vwr.value)

    low_value = round(typical_value * (1.0 - band_width), 1)
    high_value = round(typical_value * (1.0 + band_width), 1)

    band_rationale_suffix = (
        f" Band derived from typical={typical_value} ± {band_width*100:.1f}%; "
        f"the heuristic envelope covers spouse-benefit toggle (~1.5%) + "
        "mortality-table generation (~1%) without requiring per-policy details. "
        "Surface the user's exact policy mekadem via intake to replace this band."
    )

    low = ValueWithRationale(
        value=low_value,
        unit=typical_vwr.unit,
        source_id=None,  # derived
        rationale=(
            f"Favorable-band mekadem for {fund_id} (lower → higher annuity)."
            + band_rationale_suffix
        ),
        as_of_date=typical_vwr.as_of_date,
        confidence=typical_vwr.confidence,
    )
    high = ValueWithRationale(
        value=high_value,
        unit=typical_vwr.unit,
        source_id=None,
        rationale=(
            f"Unfavorable-band mekadem for {fund_id} (higher → lower annuity)."
            + band_rationale_suffix
        ),
        as_of_date=typical_vwr.as_of_date,
        confidence=typical_vwr.confidence,
    )

    return MekademBand(
        fund_id=fund_id,
        typical=typical_vwr,
        low=low,
        high=high,
    )


def monthly_annuity_for_band(
    band: MekademBand,
    *,
    balance_nis: float,
) -> tuple[ValueWithRationale, ValueWithRationale, ValueWithRationale]:
    """Convert a mekadem band into a monthly-annuity band (NIS/mo).

    Returns ``(annuity_low, annuity_typical, annuity_high)`` where
    ``annuity_low < typical < high`` — direction inverted from the
    underlying mekadem band because annuity = balance / mekadem.
    """
    if balance_nis <= 0:
        raise ValueError(f"balance_nis must be > 0, got {balance_nis}")

    def _build(mek_vwr: ValueWithRationale, label: str) -> ValueWithRationale:
        if not isinstance(mek_vwr.value, (int, float)) or mek_vwr.value <= 0:
            raise ValueError(
                f"mekadem.{label} must be a positive number; got {mek_vwr.value!r}"
            )
        monthly = round(balance_nis / float(mek_vwr.value), 2)
        return ValueWithRationale(
            value=monthly,
            unit="NIS/mo",
            source_id=None,
            rationale=(
                f"Derived: balance ₪{balance_nis:,.0f} / mekadem "
                f"{mek_vwr.value} ({label} band). "
                "See mekadem rationale for the band methodology."
            ),
            as_of_date=mek_vwr.as_of_date,
            confidence=mek_vwr.confidence,
        )

    # Inverted: low mekadem → high annuity (and vice versa)
    annuity_high = _build(band.low, "low-mekadem / high-annuity")
    annuity_typical = _build(band.typical, "typical")
    annuity_low = _build(band.high, "high-mekadem / low-annuity")
    return annuity_low, annuity_typical, annuity_high
