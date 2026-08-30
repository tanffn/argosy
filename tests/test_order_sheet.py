from datetime import UTC, date, datetime

import pytest

from argosy.services.order_sheet import (
    CandidateComparison,
    ConstraintCost,
    DatedCatalyst,
    FundingSummary,
    MarketEvidence,
    NoActionLine,
    OrderAction,
    OrderLine,
    OrderSheet,
    OutcomeExpectation,
    OutcomeScenario,
    TaxImpact,
    ThesisType,
    VoiceVerdict,
    require_valid_order_sheet,
    validate_order_sheet,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _voice(verdict: str = "BUY", source: str = "discovery") -> VoiceVerdict:
    return VoiceVerdict(
        source=source,
        verdict=verdict,
        as_of=NOW,
        rationale="fresh re-derivation",
    )


def _buy(**overrides) -> OrderLine:
    payload = dict(
        symbol="CMPS",
        action=OrderAction.BUY,
        shares=1000,
        notional_usd=10_000,
        venue="NASDAQ",
        thesis="Phase-3 readout creates asymmetric regulatory optionality.",
        thesis_type=ThesisType.CONVEXITY,
        falsifier="FDA refuses the filing or pivotal efficacy reverses.",
        catalyst=DatedCatalyst(description="FDA filing update", due_date=date(2027, 1, 31)),
        expectation=OutcomeExpectation(
            statement="Regulatory progress should rerate the equity.",
            due_date=date(2027, 2, 28),
            success_measure="total return exceeds the moonshot benchmark",
        ),
        evidence=MarketEvidence(
            price_usd=10,
            price_as_of=NOW,
            price_source="yfinance",
            market_cap_usd=1_800_000_000,
            market_cap_as_of=NOW,
            market_cap_source="yfinance",
            incorporation_country="United Kingdom",
            incorporation_as_of=NOW,
            incorporation_source="issuer filing",
        ),
        stance_source="discovery",
        voices=[_voice()],
        post_trade_weight_pct=0.25,
        expected_upside_multiple=10,
        outcome_scenarios=[
            OutcomeScenario(label="wipeout", probability_pct=30, terminal_multiple=0.1, terminal_date=date(2030, 8, 26), rationale="Clinical platform fails."),
            OutcomeScenario(label="base", probability_pct=40, terminal_multiple=1, terminal_date=date(2030, 8, 26), rationale="Mixed data leaves value unchanged."),
            OutcomeScenario(label="bull", probability_pct=20, terminal_multiple=3, terminal_date=date(2030, 8, 26), rationale="One program validates."),
            OutcomeScenario(label="moonshot", probability_pct=10, terminal_multiple=10, terminal_date=date(2030, 8, 26), rationale="Platform produces multiple winners."),
        ],
        probability_confidence="LOW",
        probability_basis="Industry clinical base rates plus current runway and program count.",
        probability_weighted_multiple=2.03,
        expected_portfolio_contribution_pct=0.25,
        capital_at_risk_pct=0.25,
        constraint_costs=[],
        estate_situs="non_US",
    )
    payload.update(overrides)
    return OrderLine(**payload)


def _sheet(*, lines=None, funding=None) -> OrderSheet:
    sheet_lines = [_buy()] if lines is None else lines
    return OrderSheet(
        user_id="ariel",
        generated_at=NOW,
        horizon_years_min=1,
        horizon_years_max=5,
        freshness_days=3,
        portfolio_symbols=["SCHD"],
        funding=funding
        or FundingSummary(
            new_cash_usd=10_000,
            gross_sell_proceeds_usd=0,
            sell_tax_usd=0,
            sell_costs_usd=0,
            reserve_usd=0,
            available_to_buy_usd=10_000,
        ),
        lines=sheet_lines,
        candidate_comparisons=[
            CandidateComparison(
                ticker=line.symbol,
                selection="SELECTED",
                radar_rank=1,
                radar_score=90.0,
                research_verdict="BUY",
                research_conviction="HIGH",
                evidence_fresh_as_of=NOW,
                key_advantage="Best catalyst-adjusted convexity among the finalists.",
                key_risk="The pivotal result can invalidate the thesis.",
                why="Selected after explicit comparison with the rejected finalists.",
                outcome_scenarios=list(line.outcome_scenarios),
                probability_confidence="LOW",
                probability_basis="Equal-basis industry rates and current evidence.",
                recommended_position_usd=line.notional_usd,
                smaller_position_usd=line.notional_usd / 2,
                why_not_smaller="The upside would not materially affect the portfolio.",
                larger_position_usd=line.notional_usd * 2,
                why_not_larger="The evidence does not warrant that much capital.",
                split_considered=True,
                split_why="Independent failure modes were compared before selecting one name.",
                sizing_why="The position loss is survivable and its weighted payoff matters.",
            )
            for line in sheet_lines
            if line.action in {OrderAction.BUY, OrderAction.ADD}
            and line.stance_source == "discovery"
        ],
        no_action=[
            NoActionLine(
                symbol="SCHD",
                reason="Conflicting voices require re-derivation.",
                voices=[_voice("HOLD", "review")],
            )
        ],
        rationale="One fully-funded list; no optional sleeve.",
    )


def test_valid_cash_only_sheet_accounts_for_every_dollar() -> None:
    report = validate_order_sheet(_sheet())
    assert report.valid is True
    assert report.buy_total_usd == 10_000


def test_zero_funding_emits_a_valid_no_action_only_sheet() -> None:
    zero = FundingSummary(
        new_cash_usd=0,
        gross_sell_proceeds_usd=0,
        sell_tax_usd=0,
        sell_costs_usd=0,
        reserve_usd=0,
        available_to_buy_usd=0,
    )

    report = validate_order_sheet(_sheet(lines=[], funding=zero))

    assert report.valid is True
    assert report.buy_total_usd == 0


def test_empty_action_list_cannot_hide_available_funding() -> None:
    report = validate_order_sheet(_sheet(lines=[]))

    assert report.valid is False
    assert "buy_total_mismatch" in {failure.code for failure in report.failures}


def test_same_row_sell_vs_hold_stops_the_sheet() -> None:
    line = _buy(voices=[_voice("SELL", "plan"), _voice("HOLD", "review")])
    report = validate_order_sheet(_sheet(lines=[line]))
    assert report.valid is False
    assert "one_voice_conflict" in {f.code for f in report.failures}


def test_prior_context_disagreement_is_retained_but_does_not_veto_current_run() -> None:
    voices = [
        _voice("SELL", "plan").model_copy(update={"decision_scope": "prior_context"}),
        _voice("HOLD", "review").model_copy(update={"decision_scope": "prior_context"}),
        _voice("BUY", "deployment_author"),
    ]
    report = validate_order_sheet(_sheet(lines=[_buy(voices=voices)]))
    assert report.valid is True, report.failures


def test_sell_funding_uses_net_after_tax_proceeds() -> None:
    sell = _buy(
        symbol="NVDA",
        action=OrderAction.TRIM,
        shares=100,
        notional_usd=20_000,
        stance_source="funding_switch",
        post_trade_weight_pct=40,
        thesis_type=ThesisType.COMPOUNDER,
        expected_upside_multiple=2,
        voices=[_voice("TRIM", "review")],
        evidence=_buy().evidence.model_copy(update={"price_usd": 200}),
        tax=TaxImpact(
            gross_proceeds_usd=20_000,
            cost_basis_usd=10_000,
            taxable_gain_usd=10_000,
            effective_tax_rate=0.25,
            estimated_tax_usd=2_500,
            net_proceeds_usd=17_500,
            method="lot basis x Israeli CGT",
            authoritative=True,
            as_of=NOW,
        ),
    )
    buy = _buy(shares=2750, notional_usd=27_500)
    funding = FundingSummary(
        new_cash_usd=10_000,
        gross_sell_proceeds_usd=20_000,
        sell_tax_usd=2_500,
        sell_costs_usd=0,
        reserve_usd=0,
        available_to_buy_usd=27_500,
    )
    assert validate_order_sheet(_sheet(lines=[sell, buy], funding=funding)).valid


def test_sell_with_gross_proceeds_as_buying_power_is_rejected() -> None:
    sell = _buy(
        symbol="NVDA",
        action=OrderAction.TRIM,
        shares=100,
        notional_usd=20_000,
        stance_source="funding_switch",
        post_trade_weight_pct=40,
        thesis_type=ThesisType.COMPOUNDER,
        expected_upside_multiple=2,
        voices=[_voice("TRIM", "review")],
        evidence=_buy().evidence.model_copy(update={"price_usd": 200}),
        tax=TaxImpact(
            gross_proceeds_usd=20_000,
            cost_basis_usd=10_000,
            taxable_gain_usd=10_000,
            effective_tax_rate=0.25,
            estimated_tax_usd=2_500,
            net_proceeds_usd=17_500,
            method="lot basis x Israeli CGT",
            authoritative=True,
            as_of=NOW,
        ),
    )
    funding = FundingSummary(
        new_cash_usd=10_000,
        gross_sell_proceeds_usd=20_000,
        sell_tax_usd=2_500,
        sell_costs_usd=0,
        reserve_usd=0,
        available_to_buy_usd=30_000,
    )
    report = validate_order_sheet(_sheet(lines=[sell, _buy()], funding=funding))
    assert "available_funds_mismatch" in {f.code for f in report.failures}


def test_sub_one_percent_reliable_two_x_is_not_an_eligible_slot() -> None:
    report = validate_order_sheet(_sheet(lines=[_buy(expected_upside_multiple=2)]))
    assert "small_position_insufficient_asymmetry" in {f.code for f in report.failures}


def test_convexity_requires_probability_distribution_and_exact_math() -> None:
    missing = _buy(
        outcome_scenarios=[],
        probability_confidence=None,
        probability_basis=None,
        probability_weighted_multiple=None,
    )
    failures = {f.code for f in validate_order_sheet(_sheet(lines=[missing])).failures}
    assert "convexity_probabilities_missing" in failures
    assert "probability_basis_missing" in failures

    # Simulate a corrupted/deserialized artifact after model construction; a
    # freshly constructed line deterministically replaces authored arithmetic.
    wrong_sheet = _sheet()
    wrong_sheet.lines[0].probability_weighted_multiple = 9.9
    failures = {f.code for f in validate_order_sheet(wrong_sheet).failures}
    assert "probability_weighted_math_mismatch" in failures


def test_scenario_returns_are_dated_annualized_and_after_tax() -> None:
    line = _buy()

    assert line.scenario_terminal_date == date(2030, 8, 26)
    assert line.probability_weighted_multiple == pytest.approx(2.03)
    assert line.after_tax_expected_multiple == pytest.approx(1.705)
    assert line.annualized_expected_return_pct == pytest.approx(19.34, abs=0.05)
    assert line.after_tax_annualized_expected_return_pct == pytest.approx(14.26, abs=0.05)
    assert line.median_terminal_multiple == 1
    assert line.probability_of_loss_pct == 30
    assert line.probability_of_near_wipeout_pct == 30


def test_convexity_requires_one_exact_terminal_date_inside_horizon() -> None:
    undated = [
        scenario.model_copy(update={"terminal_date": None})
        for scenario in _buy().outcome_scenarios
    ]
    missing = validate_order_sheet(_sheet(lines=[_buy(outcome_scenarios=undated)]))
    assert "scenario_terminal_date_missing" in {f.code for f in missing.failures}

    too_late = [
        scenario.model_copy(update={"terminal_date": date(2035, 8, 26)})
        for scenario in _buy().outcome_scenarios
    ]
    outside = validate_order_sheet(_sheet(lines=[_buy(outcome_scenarios=too_late)]))
    assert "scenario_terminal_date_out_of_horizon" in {
        f.code for f in outside.failures
    }


def test_us_situs_constraint_requires_fmv_at_event_price() -> None:
    unpriced = _buy(estate_situs="US")
    report = validate_order_sheet(_sheet(lines=[unpriced]))
    assert "unpriced_us_situs_constraint" in {f.code for f in report.failures}

    priced = _buy(
        estate_situs="US",
        constraint_costs=[
            ConstraintCost(
                constraint="us_estate_tax_exposure",
                estimated_cost_usd=4_000,
                basis_value_usd=10_000,
                valuation_basis="estimated_fmv_at_event",
                as_of=NOW,
                method="40% treaty-tail estimate on projected FMV at death",
            )
        ],
    )
    assert validate_order_sheet(_sheet(lines=[priced])).valid


def test_discovery_facts_stale_beyond_n_days_are_ineligible() -> None:
    stale = datetime(2026, 8, 20, tzinfo=UTC)
    ev = _buy().evidence.model_copy(
        update={
            "price_as_of": stale,
            "market_cap_as_of": stale,
            "incorporation_as_of": stale,
        }
    )
    report = validate_order_sheet(_sheet(lines=[_buy(evidence=ev)]))
    assert {"stale_price", "stale_market_cap", "stale_incorporation"} <= {
        f.code for f in report.failures
    }


def test_require_valid_reports_all_failures() -> None:
    with pytest.raises(ValueError, match="buy_total_mismatch"):
        require_valid_order_sheet(_sheet(lines=[_buy(notional_usd=9_000)]))
