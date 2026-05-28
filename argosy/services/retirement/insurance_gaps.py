"""Insurance gap calculators — life, disability, LTC, health supplementary
(MED #23).

For each insurance type, compute recommended_coverage from the user's
profile (income + dependents + assets), compare to actual_coverage from
intake, and surface the gap as a concrete NIS shortfall.
"""
from dataclasses import dataclass
from typing import Literal

from argosy.services.retirement.citations import ValueWithRationale


InsuranceType = Literal["life", "disability", "ltc", "health_supplementary"]


@dataclass(frozen=True)
class InsuranceGap:
    insurance_type: InsuranceType
    recommended_coverage_nis: ValueWithRationale
    actual_coverage_nis: ValueWithRationale
    gap_nis: ValueWithRationale
    suggested_action: ValueWithRationale


def compute_insurance_gaps(
    *,
    monthly_income_nis: float,
    monthly_expenses_nis: float,
    dependents_count: int,
    has_kids_under_18: bool,
    assets_nis: float,
    actual_life_coverage_nis: float = 0.0,
    actual_disability_monthly_nis: float = 0.0,
    actual_ltc_monthly_nis: float = 0.0,
    actual_health_supplementary: bool = False,
) -> list[InsuranceGap]:
    """Compute the four insurance gaps + suggested actions."""
    gaps: list[InsuranceGap] = []

    # 1) Life insurance: rule of thumb 10× annual income; deduct existing
    # assets that could substitute (only for dependents)
    if has_kids_under_18 or dependents_count > 0:
        annual_income = monthly_income_nis * 12.0
        recommended_life = max(0.0, annual_income * 10.0 - assets_nis * 0.5)
        gap_life = max(0.0, recommended_life - actual_life_coverage_nis)
        gaps.append(InsuranceGap(
            insurance_type="life",
            recommended_coverage_nis=ValueWithRationale(
                value=round(recommended_life, 2), unit="NIS",
                source_id="argosy_derived",
                rationale=(
                    "10× annual income heuristic for income replacement, "
                    "minus 50% of existing assets that could substitute "
                    "for life-insurance payout."
                ),
                confidence="medium",
            ),
            actual_coverage_nis=ValueWithRationale(
                value=actual_life_coverage_nis, unit="NIS",
                source_id="argosy_derived",
                rationale="User-supplied via intake.",
            ),
            gap_nis=ValueWithRationale(
                value=round(gap_life, 2), unit="NIS", source_id=None,
                rationale="Shortfall = recommended − actual; 0 if adequate.",
            ),
            suggested_action=ValueWithRationale(
                value=(
                    f"Increase life coverage by ₪{gap_life:,.0f}."
                    if gap_life > 0
                    else "Life insurance is adequate."
                ),
                unit="action", source_id=None,
                rationale="Concrete next step.",
            ),
        ))

    # 2) Disability income: 70% of monthly income
    recommended_disability = monthly_income_nis * 0.70
    gap_disability = max(0.0, recommended_disability - actual_disability_monthly_nis)
    gaps.append(InsuranceGap(
        insurance_type="disability",
        recommended_coverage_nis=ValueWithRationale(
            value=round(recommended_disability, 2), unit="NIS/mo",
            source_id="argosy_derived",
            rationale="70% income replacement is the common benchmark.",
        ),
        actual_coverage_nis=ValueWithRationale(
            value=actual_disability_monthly_nis, unit="NIS/mo",
            source_id="argosy_derived",
            rationale="User-supplied via intake.",
        ),
        gap_nis=ValueWithRationale(
            value=round(gap_disability, 2), unit="NIS/mo", source_id=None,
            rationale="Shortfall in monthly disability income coverage.",
        ),
        suggested_action=ValueWithRationale(
            value=(
                f"Increase monthly disability coverage by ₪{gap_disability:,.0f}/mo."
                if gap_disability > 0
                else "Disability coverage is adequate."
            ),
            unit="action", source_id=None,
            rationale="Concrete next step.",
        ),
    ))

    # 3) LTC: target ₪10K/mo at age 80+; default coverage = ₪0 if not
    # explicitly stated
    recommended_ltc = 10_000.0
    gap_ltc = max(0.0, recommended_ltc - actual_ltc_monthly_nis)
    gaps.append(InsuranceGap(
        insurance_type="ltc",
        recommended_coverage_nis=ValueWithRationale(
            value=recommended_ltc, unit="NIS/mo",
            source_id="argosy_derived",
            rationale=(
                "Israeli LTC private-care benchmark: ₪10K/mo covers in-home "
                "care or shared facility. Public + Mashlim cover less than 50%."
            ),
        ),
        actual_coverage_nis=ValueWithRationale(
            value=actual_ltc_monthly_nis, unit="NIS/mo",
            source_id="argosy_derived",
            rationale="User-supplied LTC monthly benefit.",
        ),
        gap_nis=ValueWithRationale(
            value=round(gap_ltc, 2), unit="NIS/mo", source_id=None,
            rationale="LTC coverage gap.",
        ),
        suggested_action=ValueWithRationale(
            value=(
                f"Add LTC coverage of ₪{gap_ltc:,.0f}/mo."
                if gap_ltc > 0
                else "LTC coverage is adequate."
            ),
            unit="action", source_id=None, rationale="Concrete next step.",
        ),
    ))

    # 4) Health supplementary (Mashlim) — boolean check
    gaps.append(InsuranceGap(
        insurance_type="health_supplementary",
        recommended_coverage_nis=ValueWithRationale(
            value=1, unit="boolean",
            source_id="argosy_derived",
            rationale="Bituach Mashlim is recommended for almost all Israeli households.",
        ),
        actual_coverage_nis=ValueWithRationale(
            value=int(actual_health_supplementary), unit="boolean",
            source_id="argosy_derived",
            rationale="User-supplied via intake.",
        ),
        gap_nis=ValueWithRationale(
            value=0 if actual_health_supplementary else 1,
            unit="boolean", source_id=None,
            rationale="1 = gap present; 0 = covered.",
        ),
        suggested_action=ValueWithRationale(
            value=(
                "Health supplementary (Bituach Mashlim) is in place."
                if actual_health_supplementary
                else "Add Bituach Mashlim — premium ~₪200-400/mo; covers gaps in basic basket."
            ),
            unit="action", source_id=None, rationale="Concrete next step.",
        ),
    ))

    return gaps
