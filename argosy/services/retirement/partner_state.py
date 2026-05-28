"""Partner / spouse retirement state (MED #18).

Merges spouse's pension + income + retirement age into the household-
level view. Avoids double-counting joint assets — those live in the
primary user's portfolio_snapshots already.
"""
from dataclasses import dataclass

from argosy.services.retirement.citations import ValueWithRationale


@dataclass(frozen=True)
class PartnerState:
    age_years: ValueWithRationale
    monthly_income_nis: ValueWithRationale
    pension_balance_nis: ValueWithRationale
    retirement_age: ValueWithRationale
    is_eligible_for_bl_supplement: ValueWithRationale  # boolean as 0/1


def extract_partner_state(
    *,
    age_years: float = 0.0,
    monthly_income_nis: float = 0.0,
    pension_balance_nis: float = 0.0,
    retirement_age: float = 67.0,
    is_eligible_for_bl_supplement: bool = False,
) -> PartnerState | None:
    """Build a PartnerState from intake fields. None if no partner present."""
    if age_years <= 0 and pension_balance_nis <= 0 and monthly_income_nis <= 0:
        return None
    return PartnerState(
        age_years=ValueWithRationale(
            value=age_years, unit="years", source_id="argosy_derived",
            rationale="Partner age from intake.",
        ),
        monthly_income_nis=ValueWithRationale(
            value=monthly_income_nis, unit="NIS/mo", source_id="argosy_derived",
            rationale=(
                "Partner monthly gross income. Goes into the household "
                "income side of the projection."
            ),
        ),
        pension_balance_nis=ValueWithRationale(
            value=pension_balance_nis, unit="NIS", source_id="argosy_derived",
            rationale="Partner's pension balance (kupat_pensia or equivalent).",
        ),
        retirement_age=ValueWithRationale(
            value=retirement_age, unit="years", source_id="argosy_derived",
            rationale="Partner's chosen retirement age (may differ from primary's).",
        ),
        is_eligible_for_bl_supplement=ValueWithRationale(
            value=int(is_eligible_for_bl_supplement), unit="boolean",
            source_id="bituach_leumi_old_age_2026",
            rationale=(
                "Whether the partner is eligible for the BL spouse supplement "
                "(~50% of base) — affects total household BL stipend."
            ),
        ),
    )


def household_retire_ready_age(
    *,
    primary_retire_age: float,
    partner: PartnerState | None,
) -> ValueWithRationale:
    """Household-level retirement age — typically the later of the two."""
    if partner is None:
        return ValueWithRationale(
            value=primary_retire_age, unit="years", source_id=None,
            rationale="Single-person household; primary retire age.",
        )
    partner_age = float(partner.retirement_age.value or 0.0)
    later = max(primary_retire_age, partner_age)
    return ValueWithRationale(
        value=later, unit="years", source_id=None,
        rationale=(
            f"Later of primary ({primary_retire_age}) and partner "
            f"({partner_age}) — the household isn't 'retired' until both stop."
        ),
    )
