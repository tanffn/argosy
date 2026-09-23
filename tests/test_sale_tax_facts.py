from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from argosy.services.sale_tax_facts import resolve_authoritative_sale

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("price,shares", [(219.6499, 751), (219.6499, 750), (219.64999389648438, 751)])
def test_cent_rounded_whole_share_notional_is_executable(price, shares):
    def tax_summary(session, user_id, **kwargs):
        count = kwargs["max_eligible_shares"] + kwargs["max_breaking_shares"]
        return SimpleNamespace(total_shares=count, incomplete_lot_shares=0,
            gross_at_revalue_usd=count * price, embedded_tax_at_revalue_usd=1000,
            simulation_date="2026-06-18")
    gross = round(shares * price, 2)
    result = resolve_authoritative_sale(None, user_id="ariel", symbol="NVDA",
        gross_proceeds_usd=gross, current_price_usd=price, as_of=NOW,
        tax_summary_fn=tax_summary, commission_fn=lambda g: SimpleNamespace(
            commission=25, schedule=SimpleNamespace(source="test")))
    assert result.eligible, result.failures
    assert result.quantity == shares
    assert result.net_fundable_usd == round(gross - 1025, 2)


@pytest.mark.parametrize("gross", [100.01, 100.50, 0.001])
def test_cent_rounding_does_not_allow_fractional_or_zero_share_sales(gross):
    result = resolve_authoritative_sale(None, user_id="ariel", symbol="NVDA",
        gross_proceeds_usd=gross, current_price_usd=100, as_of=NOW)
    assert not result.eligible
    assert "whole shares" in result.failures[0]


def test_nvda_sale_uses_exact_tax_engine_result_and_sourced_commission() -> None:
    calls = []

    def tax_summary(session, user_id, **kwargs):
        calls.append(kwargs)
        shares = kwargs["max_eligible_shares"] + kwargs["max_breaking_shares"]
        return SimpleNamespace(
            total_shares=shares,
            incomplete_lot_shares=0,
            gross_at_revalue_usd=shares * 200,
            embedded_tax_at_revalue_usd=2_500 if len(calls) == 2 else 2_500,
            cost_basis_at_revalue_usd=4_000,
            capital_income_at_revalue_usd=12_000,
            ordinary_income_at_revalue_usd=4_000,
            simulation_date="2026-06-18",
        )

    def commission(gross):
        return SimpleNamespace(
            commission=25,
            schedule=SimpleNamespace(source="domain_knowledge/brokers/leumi.md"),
        )

    result = resolve_authoritative_sale(
        object(),
        user_id="ariel",
        symbol="NVDA",
        gross_proceeds_usd=20_000,
        current_price_usd=200,
        as_of=NOW,
        tax_summary_fn=tax_summary,
        commission_fn=commission,
    )
    assert result.eligible
    assert result.quantity == 100
    assert result.tax is not None
    assert result.tax.calculation_basis == "trusted_tax_engine"
    assert result.tax.net_proceeds_usd == 17_500
    assert result.tax.taxable_gain_usd == 16_000
    assert result.tax.evidence_age_days == 68
    assert str(result.tax.evidence_expires_on) == "2026-09-16"
    assert result.net_fundable_usd == 17_475


def test_stale_tax_simulation_cannot_fund_a_sale() -> None:
    def tax_summary(session, user_id, **kwargs):
        shares = kwargs["max_eligible_shares"] + kwargs["max_breaking_shares"]
        return SimpleNamespace(
            total_shares=shares,
            incomplete_lot_shares=0,
            gross_at_revalue_usd=shares * 200,
            embedded_tax_at_revalue_usd=2_500,
            simulation_date="2026-05-01",
        )

    result = resolve_authoritative_sale(
        object(),
        user_id="ariel",
        symbol="NVDA",
        gross_proceeds_usd=20_000,
        current_price_usd=200,
        as_of=NOW,
        tax_summary_fn=tax_summary,
        commission_fn=lambda gross: SimpleNamespace(
            commission=0,
            schedule=SimpleNamespace(source="test"),
        ),
    )
    assert not result.eligible
    assert any("stale" in failure for failure in result.failures)


def test_unknown_symbol_never_gets_assumed_25_percent_tax() -> None:
    result = resolve_authoritative_sale(
        object(),
        user_id="ariel",
        symbol="SCHD",
        gross_proceeds_usd=20_000,
        current_price_usd=80,
        as_of=NOW,
    )
    assert not result.eligible
    assert result.net_fundable_usd is None
    assert "no authoritative tax-lot provider" in result.failures[0]
