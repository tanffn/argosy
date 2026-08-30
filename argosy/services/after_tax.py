"""Canonical after-tax sale arithmetic used by every actionable money path."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from argosy.services.order_sheet import TaxImpact


class TaxLotInput(BaseModel):
    lot_id: str = Field(min_length=1)
    quantity: float = Field(gt=0)
    cost_basis_usd: float = Field(ge=0)

    @property
    def basis_per_share(self) -> float:
        return self.cost_basis_usd / self.quantity


class SaleTaxPolicy(BaseModel):
    effective_tax_rate: float = Field(ge=0, le=1)
    method: str = Field(min_length=1)
    authoritative: bool
    source: str = Field(min_length=1)


class AfterTaxSale(BaseModel):
    tax: TaxImpact | None = None
    quantity: float = 0
    selected_lot_ids: list[str] = []
    friction_usd: float = 0
    net_fundable_usd: float | None = None
    eligible: bool = False
    failures: list[str] = []


def calculate_after_tax_sale(
    *,
    gross_proceeds_usd: float,
    current_price_usd: float,
    lots: list[TaxLotInput],
    policy: SaleTaxPolicy,
    as_of: datetime,
    friction_usd: float | None,
) -> AfterTaxSale:
    """Price an exact gross sale using HIFO lots and return net buying power.

    HIFO minimizes realized gain. Missing lots, unverified policy/FX basis, or
    unknown friction remain explicit failures; they never become zero-cost
    assumptions on an actionable order sheet.
    """

    failures: list[str] = []
    if gross_proceeds_usd <= 0 or current_price_usd <= 0:
        return AfterTaxSale(failures=["gross proceeds and current price must be positive"])
    if not lots:
        return AfterTaxSale(failures=["no tax lots available"])
    if not policy.authoritative:
        failures.append("tax policy/basis is not authoritative")
    if friction_usd is None:
        failures.append("commission/spread/FX friction is unresolved")

    shares_needed = gross_proceeds_usd / current_price_usd
    remaining = shares_needed
    selected_basis = 0.0
    selected_ids: list[str] = []
    for lot in sorted(lots, key=lambda row: row.basis_per_share, reverse=True):
        if remaining <= 1e-9:
            break
        take = min(lot.quantity, remaining)
        selected_basis += take * lot.basis_per_share
        selected_ids.append(lot.lot_id)
        remaining -= take
    if remaining > 1e-6:
        failures.append(f"tax lots are short by {remaining:.6f} shares")

    taxable_gain = max(0.0, gross_proceeds_usd - selected_basis)
    estimated_tax = taxable_gain * policy.effective_tax_rate
    net_proceeds = gross_proceeds_usd - estimated_tax
    tax = TaxImpact(
        gross_proceeds_usd=round(gross_proceeds_usd, 2),
        cost_basis_usd=round(selected_basis, 2),
        taxable_gain_usd=round(taxable_gain, 2),
        effective_tax_rate=policy.effective_tax_rate,
        estimated_tax_usd=round(estimated_tax, 2),
        net_proceeds_usd=round(net_proceeds, 2),
        method=f"{policy.method}; {policy.source}; HIFO lots",
        authoritative=policy.authoritative,
        as_of=as_of,
    )
    friction = float(friction_usd or 0.0)
    return AfterTaxSale(
        tax=tax,
        quantity=round(shares_needed - max(remaining, 0.0), 8),
        selected_lot_ids=selected_ids,
        friction_usd=friction,
        net_fundable_usd=round(net_proceeds - friction, 2),
        eligible=not failures,
        failures=failures,
    )


def sale_fraction_to_recover_original_capital(
    *,
    expected_multiple: float,
    effective_tax_rate: float,
) -> float:
    """Fraction of the future position that nets the original investment.

    At 2x with 25% CGT on the gain: ``1 / (2 - .25*(2-1)) = 57.1429%``.
    """

    if expected_multiple <= 1:
        raise ValueError("expected_multiple must exceed 1")
    if not 0 <= effective_tax_rate <= 1:
        raise ValueError("effective_tax_rate must be between 0 and 1")
    net_value_per_original_dollar = expected_multiple - effective_tax_rate * (
        expected_multiple - 1.0
    )
    return 1.0 / net_value_per_original_dollar


__all__ = [
    "AfterTaxSale",
    "SaleTaxPolicy",
    "TaxLotInput",
    "calculate_after_tax_sale",
    "sale_fraction_to_recover_original_capital",
]
