"""Healthcare cost curve for Israeli households.

Closes MED #22 from the 2026-05-28 SDD review. Israeli health basket
(Kupat Holim) covers core care; supplementary insurance (Bituach Mashlim)
covers gaps; private insurance for premium care. At age 65+, healthcare
becomes a material % of household burn.

Cost model (simplified, NIS/mo):
  age <55:   ₪600/mo  (Bituach Mashlim + dental + dental insurance)
  55-65:     ₪900/mo  (supplementary up + medication ramp)
  65-75:     ₪1500/mo (Mashlim premium ages up; medications)
  75-85:     ₪2500/mo (more medications + occasional procedures)
  85+:       ₪4000/mo (LTC creep; in-home care; daily medications)

These are nominal NIS/mo additions on top of baseline burn. Inflation
adds ~1.5%/yr above CPI per OECD Israel data (model in phase_expenses
via inflation_premium).

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 4 MED #22.
"""
from dataclasses import dataclass

from argosy.services.retirement.citations import ValueWithRationale


@dataclass(frozen=True)
class HealthcareCurvePoint:
    age: int
    monthly_cost_nis: ValueWithRationale


_BAND_TABLE: list[tuple[int, int, float]] = [
    (0, 55, 600.0),
    (55, 65, 900.0),
    (65, 75, 1500.0),
    (75, 85, 2500.0),
    (85, 120, 4000.0),
]


def _cost_at_age(age: int) -> float:
    for lo, hi, cost in _BAND_TABLE:
        if lo <= age < hi:
            return cost
    return _BAND_TABLE[-1][2]


def build_healthcare_curve(
    *,
    start_age: int = 30,
    end_age: int = 95,
) -> list[HealthcareCurvePoint]:
    """Return the age-banded healthcare cost curve."""
    out: list[HealthcareCurvePoint] = []
    for age in range(start_age, end_age + 1):
        cost = _cost_at_age(age)
        out.append(HealthcareCurvePoint(
            age=age,
            monthly_cost_nis=ValueWithRationale(
                value=cost,
                unit="NIS/mo",
                source_id="argosy_derived",
                rationale=(
                    f"Israeli household healthcare expense at age {age}: "
                    f"Bituach Mashlim + supplementary insurance + medications + "
                    f"(post-65) increased Mashlim premiums."
                ),
                alternatives_considered=[
                    "OECD Israel data suggests +1.5%/yr real growth post-65 — "
                    "applied via phase_expenses inflation_premium.",
                ],
                confidence="medium",
            ),
        ))
    return out


def healthcare_share_of_burn(
    *,
    age: int,
    monthly_burn_nis: float,
) -> ValueWithRationale:
    """Healthcare cost as % of household monthly burn at the given age."""
    if monthly_burn_nis <= 0:
        return ValueWithRationale(
            value=None,
            unit="fraction",
            source_id=None,
            rationale="No household burn data available.",
            confidence="low",
        )
    cost = _cost_at_age(age)
    share = cost / monthly_burn_nis
    return ValueWithRationale(
        value=round(share, 4),
        unit="fraction",
        source_id="argosy_derived",
        rationale=(
            f"Healthcare ₪{cost:,.0f}/mo as a fraction of household burn "
            f"₪{monthly_burn_nis:,.0f}/mo at age {age}."
        ),
        confidence="medium",
    )
