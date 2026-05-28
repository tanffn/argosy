"""Bituach Leumi (Israeli social security) old-age stipend estimator.

Closes HIGH #6 from the 2026-05-28 SDD review: the projection was excluding
the BL old-age stipend, biasing retirement age later than it should be.

Eligibility (simplified, suitable for projection use; the BL website is the
authoritative source per ``bituach_leumi_old_age_2026``):
  - Single base rate at age 67 with full contribution history (35+ insured
    years). Reduced proportionally for shorter histories down to a minimum
    floor (~50% of base at very short histories).
  - Spouse supplement: ~50% of base if spouse is eligible (separate intake
    field). Couples can also receive two independent stipends if both have
    sufficient insured years.
  - Means-tested supplements for low-income households exist but are out of
    scope for the retirement projection (Argosy's user profile is not in
    the target population for those supplements).

This module returns a ``BLStipendEstimate`` with low/typical/high bands +
sensitivity levers for the SensitivityPanel UI primitive.

Plan: `docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md` § Wave 1.
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.retirement.reference import resolve

# Minimum stipend as a fraction of base for users with sparse contribution
# history. BL formula: stipend scales linearly with insured-years up to 35,
# floored at this minimum for very-short histories.
_MIN_STIPEND_FRACTION = 0.50

# Full-history threshold in years.
_FULL_HISTORY_YEARS = 35


@dataclass(frozen=True)
class BLStipendEstimate:
    """Per-month BL stipend estimate at the eligibility age."""
    monthly_nis: ValueWithRationale  # central estimate
    monthly_nis_low: ValueWithRationale  # pessimistic band (no spouse, slight history shortfall)
    monthly_nis_high: ValueWithRationale  # optimistic band (spouse supplement included)
    eligibility_age: ValueWithRationale
    contribution_history_factor: ValueWithRationale  # 0.0-1.0 multiplier applied
    spouse_supplement_applied: ValueWithRationale  # bool as value
    sensitivity_levers: list[dict]


def _scale_for_history(years: int) -> float:
    """Return the fractional multiplier for an incomplete contribution history.

    Linear scale from MIN_STIPEND_FRACTION (at 0 insured years) to 1.0
    (at FULL_HISTORY_YEARS). Capped at 1.0 above the full-history threshold.
    """
    if years <= 0:
        return _MIN_STIPEND_FRACTION
    if years >= _FULL_HISTORY_YEARS:
        return 1.0
    return _MIN_STIPEND_FRACTION + (
        (1.0 - _MIN_STIPEND_FRACTION) * (years / _FULL_HISTORY_YEARS)
    )


def estimate_bl_stipend(
    *,
    current_age: int,
    contribution_history_years: int,
    spouse_eligible: bool,
    user_id: str,
    session: Session,
) -> BLStipendEstimate:
    """Estimate the user's monthly BL old-age stipend at the eligibility age.

    Returns a ``BLStipendEstimate`` with:
      - ``monthly_nis``: central estimate at full-eligibility age 67
      - ``monthly_nis_low``: same minus spouse supplement (if spouse_eligible),
        and with a -10% conservative shading on the base
      - ``monthly_nis_high``: same plus spouse supplement (if eligible),
        and with a +5% optimistic shading
      - ``contribution_history_factor``: fraction applied for incomplete history
      - ``spouse_supplement_applied``: whether the supplement was added
      - ``sensitivity_levers``: top-3 levers for the SensitivityPanel
    """
    base_vwr = resolve(
        "bituach_leumi.single_age_67_base_2026",
        user_id=user_id, session=session,
    )
    spouse_pct_vwr = resolve(
        "bituach_leumi.spouse_supplement_pct",
        user_id=user_id, session=session,
    )
    base = float(base_vwr.value or 0.0)
    spouse_pct = float(spouse_pct_vwr.value or 0.0)

    history_factor = _scale_for_history(contribution_history_years)
    spouse_applied = bool(spouse_eligible)

    central = round(
        base * history_factor * (1.0 + (spouse_pct if spouse_applied else 0.0)),
        2,
    )
    # Low: no spouse + 10% conservative shading on history
    low = round(base * history_factor * 0.90, 2)
    # High: with spouse + 5% optimistic shading
    high = round(
        base * history_factor * 1.05 * (1.0 + (spouse_pct if spouse_applied else 0.0)),
        2,
    )

    def _wrap(v: float, label: str) -> ValueWithRationale:
        return ValueWithRationale(
            value=v,
            unit="NIS/mo",
            source_id="bituach_leumi_old_age_2026",
            rationale=(
                f"BL old-age stipend ({label}) at age 67. "
                f"Base ₪{base:,.0f}/mo × history factor "
                f"{history_factor:.2f} (history {contribution_history_years}y "
                f"of {_FULL_HISTORY_YEARS}y full)"
                + (
                    f", + {spouse_pct*100:.0f}% spouse supplement"
                    if spouse_applied
                    else ", no spouse supplement"
                )
                + "."
            ),
            as_of_date=base_vwr.as_of_date,
            confidence=base_vwr.confidence,
            freshness_warning=base_vwr.freshness_warning,
        )

    levers = [
        {
            "name": "Contribute the remaining years to full eligibility",
            "delta_nis_per_mo": round(
                base * (1.0 - history_factor)
                * (1.0 + (spouse_pct if spouse_applied else 0.0)),
                2,
            ),
            "source_id": "bituach_leumi_old_age_2026",
        },
        {
            "name": "Spouse supplement (if eligible & not currently counted)",
            "delta_nis_per_mo": (
                0.0 if spouse_applied
                else round(base * history_factor * spouse_pct, 2)
            ),
            "source_id": "bituach_leumi_old_age_2026",
        },
        {
            "name": "Delay claiming past 67 (~5% boost per delayed year)",
            "delta_nis_per_mo": round(central * 0.05, 2),  # rough per-year boost
            "source_id": "argosy_derived",
        },
    ]

    return BLStipendEstimate(
        monthly_nis=_wrap(central, "central estimate"),
        monthly_nis_low=_wrap(low, "low band"),
        monthly_nis_high=_wrap(high, "high band"),
        eligibility_age=ValueWithRationale(
            value=67,
            unit="years",
            source_id="bituach_leumi_old_age_2026",
            rationale=(
                "Israeli statutory old-age claim age. Delay options exist; "
                "claiming earlier than 67 with reduced stipend is also possible "
                "for some categories (out of scope for this projection)."
            ),
            confidence="high",
        ),
        contribution_history_factor=ValueWithRationale(
            value=round(history_factor, 4),
            unit="fraction",
            source_id="bituach_leumi_old_age_2026",
            rationale=(
                f"Linear scale from {_MIN_STIPEND_FRACTION:.2f} "
                f"(0 insured years) to 1.0 ({_FULL_HISTORY_YEARS}+ years). "
                f"User history: {contribution_history_years} years."
            ),
            confidence="medium",
        ),
        spouse_supplement_applied=ValueWithRationale(
            value=int(spouse_applied),
            unit="boolean",
            source_id=None,
            rationale=(
                "User intake: spouse eligible for separate BL stipend (no supplement applied)."
                if not spouse_applied
                else "User intake: spouse eligible for supplement, ~50% of base added."
            ),
            confidence="high",
        ),
        sensitivity_levers=levers,
    )
