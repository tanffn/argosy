from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from argosy.services.after_tax import AfterTaxSale
from argosy.services.order_sheet import TaxImpact
from argosy.services.staged_sell_policy import build_nvda_staged_sell_policy


def test_staged_policy_joins_glide_pace_and_after_tax_tranche() -> None:
    observed = datetime(2026, 8, 29, 12, tzinfo=UTC)
    assessment = SimpleNamespace(
        status="sell_due",
        category="policy",
        tranche_nis=300_000.0,
        n_quarters=4,
        nvda_current_pct=55.0,
        nvda_cap_pct=13.0,
        headline="Sell one quarterly tranche.",
        tax_note="Section-102 eligible lots first.",
        notes=("Reassess after the fill.",),
    )
    pace = SimpleNamespace(
        status="on",
        next_waypoint_date=date(2026, 11, 24),
        next_waypoint_weight_pct=45.5,
        basis="glide",
        shares_to_sell_by_waypoint=450,
        tax_year=2026,
        annual_flow=7_077,
        sold_calendar_ytd=3_940,
        target_shares=4_062,
    )

    def resolve(*args, **kwargs):
        gross = kwargs["gross_proceeds_usd"]
        return AfterTaxSale(
            eligible=True,
            quantity=gross / 200.0,
            friction_usd=5.0,
            net_fundable_usd=gross * 0.75 - 5.0,
            tax=TaxImpact(
                gross_proceeds_usd=gross,
                effective_tax_rate=0.25,
                estimated_tax_usd=gross * 0.25,
                net_proceeds_usd=gross * 0.75,
                method="trusted Section-102 test",
                authoritative=True,
                as_of=observed,
            ),
        )

    policy = build_nvda_staged_sell_policy(
        object(),
        user_id="ariel",
        current_price_usd=200.0,
        fx_usd_nis=3.0,
        as_of=observed,
        assessment_fn=lambda **kwargs: assessment,
        pace_fn=lambda *args, **kwargs: pace,
        eligible_shares_fn=lambda *args, **kwargs: 9_230.0,
        sale_resolver_fn=resolve,
    )

    assert policy is not None
    assert policy["status"] == "actionable"
    assert policy["full_liquidation_forbidden"] is True
    assert policy["recommended_current_tranche_usd"] == 30_000.0
    assert policy["maximum_current_tranche_usd"] == 30_000.0
    assert policy["recommended_current_tranche_shares"] == 150.0
    assert policy["shares_to_sell_by_next_waypoint"] == 450
    assert [clip["shares"] for clip in policy["clips"]] == [150, 150, 150]
    assert policy["sizing_basis"] == "canonical_dated_glide_waypoint"
    assert policy["execute_no_later_than"] == "2026-09-24"
    assert policy["next_review_date"] == "2026-09-24"
    assert policy["estimated_tax_usd"] == 7_500.0
    assert policy["estimated_net_fundable_usd"] == 22_495.0


def test_staged_policy_returns_no_action_instead_of_fabricating_sale() -> None:
    assessment = SimpleNamespace(
        status="no_action",
        tranche_nis=0.0,
        headline="Within cap; no tranche due.",
    )
    policy = build_nvda_staged_sell_policy(
        object(),
        user_id="ariel",
        current_price_usd=200.0,
        fx_usd_nis=3.0,
        assessment_fn=lambda **kwargs: assessment,
    )
    assert policy is not None
    assert policy["status"] == "no_action"
    assert "recommended_current_tranche_usd" not in policy


def test_waypoint_tranche_adds_shares_for_after_tax_denominator() -> None:
    observed = datetime(2026, 8, 29, 12, tzinfo=UTC)
    assessment = SimpleNamespace(
        status="sell_due",
        category="policy",
        tranche_nis=300_000.0,
        n_quarters=4,
        nvda_current_pct=55.0,
        nvda_cap_pct=13.0,
        headline="Quarterly trim.",
        tax_note="Taxed.",
        notes=(),
    )
    pace = SimpleNamespace(
        status="on",
        basis="glide",
        next_waypoint_date=date(2026, 11, 24),
        next_waypoint_weight_pct=45.0,
        shares_to_sell_by_waypoint=1_000,
        tax_year=2026,
        annual_flow=4_000,
        sold_calendar_ytd=0,
        target_shares=0,
    )

    def resolve(*args, **kwargs):
        gross = kwargs["gross_proceeds_usd"]
        tax = gross * 0.25
        return AfterTaxSale(
            eligible=True,
            quantity=gross / 100.0,
            friction_usd=0.0,
            net_fundable_usd=gross - tax,
            tax=TaxImpact(
                gross_proceeds_usd=gross,
                effective_tax_rate=0.25,
                estimated_tax_usd=tax,
                net_proceeds_usd=gross - tax,
                method="trusted test tax",
                authoritative=True,
                as_of=observed,
            ),
        )

    policy = build_nvda_staged_sell_policy(
        object(),
        user_id="ariel",
        current_price_usd=100.0,
        fx_usd_nis=3.0,
        current_nvda_value_usd=550_000.0,
        book_usd=1_000_000.0,
        as_of=observed,
        assessment_fn=lambda **kwargs: assessment,
        pace_fn=lambda *args, **kwargs: pace,
        eligible_shares_fn=lambda *args, **kwargs: 10_000.0,
        sale_resolver_fn=resolve,
    )

    assert policy is not None
    assert policy["sizing_basis"] == "after_tax_dated_glide_waypoint"
    assert policy["glide_base_shares_to_sell_by_next_waypoint"] == 1_000
    assert policy["recommended_current_tranche_shares"] == 376
    assert policy["shares_to_sell_by_next_waypoint"] == 1_127
    assert policy["tax_denominator_adjustment_shares"] == 127
    assert [clip["shares"] for clip in policy["clips"]] == [376, 376, 375]
    assert policy["estimated_post_trade_weight_pct"] > 45.0
