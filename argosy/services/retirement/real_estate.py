"""Real-estate equity in net worth (MED #16).

Israeli historical home appreciation ~3.5%/yr nominal (Bank of Israel
2000-2024 median TLV metro). Conservative central estimate; user can
override via identity_yaml.real_estate intake.
"""
from dataclasses import dataclass

from argosy.services.retirement.citations import ValueWithRationale


@dataclass(frozen=True)
class RealEstateState:
    primary_residence_value_nis: ValueWithRationale
    mortgage_balance_nis: ValueWithRationale
    equity_nis: ValueWithRationale
    appreciation_annual: ValueWithRationale
    illiquidity_haircut: ValueWithRationale
    monthly_property_tax_nis: ValueWithRationale


def extract_real_estate_state(
    *,
    primary_residence_value_nis: float = 0.0,
    mortgage_balance_nis: float = 0.0,
    monthly_property_tax_nis: float = 0.0,
    appreciation_annual: float = 0.035,
    illiquidity_haircut: float = 0.10,
) -> RealEstateState:
    equity = max(0.0, primary_residence_value_nis - mortgage_balance_nis)
    return RealEstateState(
        primary_residence_value_nis=ValueWithRationale(
            value=primary_residence_value_nis, unit="NIS",
            source_id="argosy_derived",
            rationale="User-supplied via intake or 0 if no primary residence.",
        ),
        mortgage_balance_nis=ValueWithRationale(
            value=mortgage_balance_nis, unit="NIS",
            source_id="argosy_derived",
            rationale="Outstanding mortgage principal.",
        ),
        equity_nis=ValueWithRationale(
            value=equity, unit="NIS", source_id=None,
            rationale="Value − mortgage balance.",
        ),
        appreciation_annual=ValueWithRationale(
            value=appreciation_annual, unit="fraction",
            source_id="argosy_derived",
            rationale=(
                "Israeli historical home price appreciation ~3.5%/yr "
                "nominal (Bank of Israel 2000-2024 median TLV metro)."
            ),
            confidence="medium",
        ),
        illiquidity_haircut=ValueWithRationale(
            value=illiquidity_haircut, unit="fraction",
            source_id="argosy_derived",
            rationale=(
                "10% haircut applied to value when computing usable net "
                "worth — reflects transaction costs + time-to-sell."
            ),
        ),
        monthly_property_tax_nis=ValueWithRationale(
            value=monthly_property_tax_nis, unit="NIS/mo",
            source_id="argosy_derived",
            rationale="Arnona / property tax monthly.",
        ),
    )
