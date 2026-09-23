from datetime import UTC, date, datetime

import pytest

from argosy.services.after_tax import AfterTaxSale
from argosy.services.allocation_author.proposal import (
    AllocationProposal,
    AuthoredOrderIntent,
    Buy,
    Sell,
)
from argosy.services.order_sheet import (
    CandidateComparison,
    MarketEvidence,
    NoActionLine,
    TaxImpact,
    VoiceVerdict,
)
from argosy.services.order_sheet_builder import (
    ExecutionFacts,
    build_cash_order_sheet,
    build_order_sheet,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _intent() -> AuthoredOrderIntent:
    return AuthoredOrderIntent(
        thesis="Own a true ex-US diversifier to close the plan gap.",
        thesis_type="diversifier",
        falsifier="The fund becomes materially US-heavy.",
        catalyst_description="Next quarterly rebalance",
        catalyst_date="2026-11-30",
        expectation="Ex-US sleeve approaches its governing target.",
        expectation_due_date="2026-12-31",
        success_measure="gap closes without increasing NVDA look-through",
        expected_upside_multiple=2,
    )


def _voice(verdict="HOLD") -> VoiceVerdict:
    return VoiceVerdict(
        source="review",
        verdict=verdict,
        as_of=NOW,
        rationale="fresh review",
    )


@pytest.mark.parametrize("fund_verdict", ["HOLD", "TRIM"])
@pytest.mark.parametrize("selected", [True, False])
def test_authored_cash_allocation_becomes_validated_quantities(fund_verdict, selected) -> None:
    proposal = AllocationProposal(
        cash_to_deploy=120_000,
        buys=[
            Buy(
                symbol="EXUS",
                amount_usd=120_000,
                sleeve="Ex-US developed",
                justification="closes the ex-US gap",
                claimed_us_weight=0,
                order_intent=_intent(),
            )
        ],
        rationale="One list, fully deploy the stated cash.",
        candidate_comparisons=[CandidateComparison(ticker="EXUS" if selected else "IWQU",
            selection="SELECTED" if selected else "NOT_SELECTED", research_verdict=fund_verdict,
            research_conviction="MED", evidence_fresh_as_of=NOW, key_advantage="Diversified vehicle",
            key_risk="Index tracking", why="Compared using the fund's mandate and portfolio role",
            recommended_position_usd=120000 if selected else 0)],
    )
    facts = ExecutionFacts(
        evidence=MarketEvidence(
            price_usd=24,
            price_as_of=NOW,
            price_source="yfinance live retrieval",
            incorporation_country="IE",
            incorporation_as_of=NOW,
            incorporation_source="canonical plan instrument domicile",
        ),
        venue="LSE",
        estate_situs="non_US",
    )
    built = build_cash_order_sheet(
        proposal,
        user_id="ariel",
        new_cash_usd=120_000,
        holdings_usd={"SCHD": 50_000},
        book_usd=1_000_000,
        facts_by_symbol={"EXUS": facts},
        no_action=[
            NoActionLine(
                symbol="SCHD",
                reason="Fresh review retained the position.",
                voices=[_voice()],
            )
        ],
        generated_at=NOW,
    )
    assert built.validation.valid, built.validation.failures
    assert built.sheet.lines[0].shares == 5_000
    assert built.sheet.lines[0].action == "BUY"
    assert built.validation.buy_total_usd == 120_000
    from argosy.services.order_sheet import validate_order_sheet
    wrong = built.sheet.model_copy(deep=True)
    wrong.candidate_comparisons[0].recommended_position_usd += 100
    assert any(f.code == "candidate_sizing_order_mismatch" for f in validate_order_sheet(wrong).failures)
    wrong.candidate_comparisons[0].recommended_position_usd -= 100
    wrong.candidate_comparisons[0].evidence_fresh_as_of = datetime(2026, 1, 1, tzinfo=UTC)
    assert any(f.code == "fund_comparison_evidence_missing" for f in validate_order_sheet(wrong).failures)


def test_missing_author_judgment_never_becomes_an_order() -> None:
    proposal = AllocationProposal(
        cash_to_deploy=120_000,
        buys=[Buy(symbol="EXUS", amount_usd=120_000, claimed_us_weight=0)],
        rationale="x",
    )
    with pytest.raises(ValueError, match="missing authored order_intent"):
        build_cash_order_sheet(
            proposal,
            user_id="ariel",
            new_cash_usd=120_000,
            holdings_usd={},
            book_usd=1_000_000,
            facts_by_symbol={},
            no_action=[
                NoActionLine(
                    symbol="CASH",
                    reason="not used",
                    voices=[_voice()],
                )
            ],
            generated_at=NOW,
        )


def test_sell_to_buy_sheet_uses_resolved_net_proceeds() -> None:
    proposal = AllocationProposal(
        cash_to_deploy=27_475,
        buys=[
            Buy(
                symbol="EXUS",
                amount_usd=27_475,
                claimed_us_weight=0,
                order_intent=_intent(),
            )
        ],
        sells=[
            Sell(
                symbol="NVDA",
                amount_usd=20_000,
                reason="fund the higher-conviction diversification move",
                execution_style="staged_tranche",
                execute_by=date(2026, 9, 5),
                next_review_date=date(2026, 11, 24),
                tranche_reason="One glide tranche, then refresh price and tax.",
                order_intent=_intent(),
            )
        ],
        rationale="Use new cash plus verified after-tax trim proceeds.",
    )
    buy_facts = ExecutionFacts(
        evidence=MarketEvidence(
            price_usd=10,
            price_as_of=NOW,
            price_source="live",
            incorporation_country="IE",
            incorporation_as_of=NOW,
            incorporation_source="plan",
        ),
        venue="LSE",
        estate_situs="non_US",
    )
    sell_facts = ExecutionFacts(
        evidence=MarketEvidence(
            price_usd=200,
            price_as_of=NOW,
            price_source="live",
        ),
        venue="NASDAQ",
        estate_situs="US",
    )
    resolution = AfterTaxSale(
        tax=TaxImpact(
            gross_proceeds_usd=20_000,
            calculation_basis="trusted_tax_engine",
            cost_basis_usd=4_000,
            taxable_gain_usd=16_000,
            capital_income_usd=12_000,
            ordinary_income_usd=4_000,
            effective_tax_rate=0.125,
            estimated_tax_usd=2_500,
            net_proceeds_usd=17_500,
            method="trusted Section-102 engine",
            authoritative=True,
            as_of=NOW,
            evidence_as_of=date(2026, 6, 18),
            evidence_age_days=68,
            evidence_max_age_days=90,
            evidence_expires_on=date(2026, 9, 16),
        ),
        quantity=100,
        friction_usd=25,
        net_fundable_usd=17_475,
        eligible=True,
    )
    built = build_order_sheet(
        proposal,
        user_id="ariel",
        new_cash_usd=10_000,
        holdings_usd={"NVDA": 50_000, "SCHD": 50_000},
        book_usd=1_000_000,
        facts_by_symbol={"EXUS": buy_facts, "NVDA": sell_facts},
        no_action=[
            NoActionLine(
                symbol="SCHD",
                reason="Fresh review retained the position.",
                voices=[_voice()],
            )
        ],
        voices_by_symbol={"NVDA": [_voice("TRIM")]},
        sale_resolutions={"NVDA": resolution},
        staged_sell_policies={
            "NVDA": {
                "maximum_current_tranche_usd": 20_000,
                "remaining_glide_quarters": 4,
                "next_waypoint_date": "2026-11-24",
                "next_waypoint_weight_pct": 45.5,
                "shares_to_sell_by_next_waypoint": 100,
                "reassess_after_fill": True,
            }
        },
        generated_at=NOW,
    )
    assert built.validation.valid, built.validation.failures
    assert built.sheet.funding.sell_tax_usd == 2_500
    assert built.sheet.funding.sell_costs_usd == 25
    assert built.validation.buy_total_usd == 27_470
    assert built.sheet.funding.rounding_residual_usd == 5
    sell_line = next(line for line in built.sheet.lines if line.symbol == "NVDA")
    assert sell_line.stance_source == "portfolio_review"
    assert sell_line.staged_execution is not None
    assert sell_line.staged_execution.execute_by == date(2026, 9, 5)
    assert sell_line.staged_execution.next_review_date == date(2026, 11, 24)
