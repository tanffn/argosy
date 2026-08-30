from datetime import UTC, datetime

import pytest

from argosy.services.after_tax import (
    SaleTaxPolicy,
    TaxLotInput,
    calculate_after_tax_sale,
    sale_fraction_to_recover_original_capital,
)

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def test_two_x_sale_needs_57_1_percent_to_recover_capital_after_cgt() -> None:
    fraction = sale_fraction_to_recover_original_capital(
        expected_multiple=2,
        effective_tax_rate=0.25,
    )
    assert fraction == pytest.approx(4 / 7)
    assert fraction * 100 == pytest.approx(57.142857)


def test_hifo_sale_returns_net_fundable_not_gross() -> None:
    result = calculate_after_tax_sale(
        gross_proceeds_usd=20_000,
        current_price_usd=200,
        lots=[
            TaxLotInput(lot_id="low", quantity=100, cost_basis_usd=5_000),
            TaxLotInput(lot_id="high", quantity=100, cost_basis_usd=10_000),
        ],
        policy=SaleTaxPolicy(
            effective_tax_rate=0.25,
            method="Israeli CGT on realized gain",
            authoritative=True,
            source="verified lot/FX tax layer",
        ),
        as_of=NOW,
        friction_usd=50,
    )
    assert result.eligible
    assert result.selected_lot_ids == ["high"]
    assert result.tax is not None
    assert result.tax.estimated_tax_usd == 2_500
    assert result.net_fundable_usd == 17_450


def test_unverified_tax_or_unknown_friction_never_becomes_buying_power() -> None:
    result = calculate_after_tax_sale(
        gross_proceeds_usd=20_000,
        current_price_usd=200,
        lots=[TaxLotInput(lot_id="x", quantity=100, cost_basis_usd=10_000)],
        policy=SaleTaxPolicy(
            effective_tax_rate=0.25,
            method="fallback",
            authoritative=False,
            source="USD basis only",
        ),
        as_of=NOW,
        friction_usd=None,
    )
    assert not result.eligible
    assert "tax policy/basis is not authoritative" in result.failures
    assert "commission/spread/FX friction is unresolved" in result.failures
