"""The daily proactive money loop (period_directive_daily): deterministic triage
→ fleet compose (stubbed — never a live LLM here) → one-row inbox sink with
refresh-in-place + auto-supersede. Real-schema writes go through the migrated DB
(the fake-db lesson of migration 0077: a CHECK failure looks exactly like a
dedup collision to a sink that swallows IntegrityError)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.orm import Session

from argosy.services.allocation_author.proposal import Buy
from argosy.services.jobs.period_directive_daily import (
    PeriodDirectiveDailyJob,
    compose_authored_directive,
    period_directive_daily_metadata,
    run_period_directive_daily,
)
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
    validate_order_sheet,
)
from argosy.state.models import ActionProposal, HoldingReview, Proposal, ProposalHistory


def _event(excess_usd: float) -> SimpleNamespace:
    return SimpleNamespace(excess_usd=excess_usd)


def _accepted(*buys: Buy) -> SimpleNamespace:
    now = datetime.now(UTC)
    total = sum(b.amount_usd for b in buys)
    lines = [
        OrderLine(
            symbol=buy.symbol,
            action=OrderAction.BUY,
            shares=buy.amount_usd / 10,
            notional_usd=buy.amount_usd,
            venue="NASDAQ",
            thesis="Fresh evidence supports the selected plan role.",
            thesis_type=ThesisType.CONVEXITY,
            falsifier="The dated evidence fails to confirm the thesis.",
            catalyst=DatedCatalyst(
                description="Scheduled thesis checkpoint", due_date=date(2027, 1, 31)
            ),
            expectation=OutcomeExpectation(
                statement="The position should outperform its sleeve benchmark.",
                due_date=date(2027, 2, 28),
                success_measure="total return exceeds sleeve benchmark",
            ),
            evidence=MarketEvidence(
                price_usd=10,
                price_as_of=now,
                price_source="live-test-seam",
                market_cap_usd=2_000_000_000,
                market_cap_as_of=now,
                market_cap_source="live-test-seam",
                incorporation_country="Ireland",
                incorporation_as_of=now,
                incorporation_source="issuer",
            ),
            stance_source="discovery",
            voices=[VoiceVerdict(
                source="deployment_author", verdict="BUY", as_of=now,
                rationale="current run",
            )],
            post_trade_weight_pct=max(1.0, buy.amount_usd / 1_000_000 * 100),
            expected_upside_multiple=5,
            outcome_scenarios=[
                OutcomeScenario(label="wipeout", probability_pct=30, terminal_multiple=0.1, terminal_date=date(2030, 8, 26), rationale="Thesis fails."),
                OutcomeScenario(label="base", probability_pct=50, terminal_multiple=1, terminal_date=date(2030, 8, 26), rationale="Mixed outcome."),
                OutcomeScenario(label="upside", probability_pct=20, terminal_multiple=5, terminal_date=date(2030, 8, 26), rationale="Program succeeds."),
            ],
            probability_confidence="LOW",
            probability_basis="Clinical base rates and current evidence.",
            probability_weighted_multiple=1.53,
            expected_portfolio_contribution_pct=0.1,
            capital_at_risk_pct=max(1.0, buy.amount_usd / 1_000_000 * 100),
            estate_situs="non_US",
        )
        for buy in buys
    ]
    sheet = OrderSheet(
        user_id="ariel",
        generated_at=now,
        horizon_years_min=1,
        horizon_years_max=5,
        freshness_days=3,
        portfolio_symbols=["SCHD"],
        funding=FundingSummary(
            new_cash_usd=total,
            gross_sell_proceeds_usd=0,
            sell_tax_usd=0,
            sell_costs_usd=0,
            reserve_usd=0,
            available_to_buy_usd=total,
        ),
        lines=lines,
        candidate_comparisons=[CandidateComparison(
            ticker=buy.symbol,
            selection="SELECTED",
            radar_rank=index,
            radar_score=80.0,
            research_verdict="BUY",
            research_conviction="MED",
            evidence_fresh_as_of=now,
            key_advantage="Best comparative fit for the funded sleeve.",
            key_risk="The dated thesis can fail.",
            why="Selected by the authored comparison for this test run.",
            outcome_scenarios=list(lines[index - 1].outcome_scenarios),
            probability_confidence="LOW",
            probability_basis="Equal-basis industry rates and current evidence.",
            recommended_position_usd=buy.amount_usd,
            smaller_position_usd=buy.amount_usd / 2,
            why_not_smaller="The upside would not materially affect the portfolio.",
            larger_position_usd=buy.amount_usd * 2,
            why_not_larger="The evidence does not warrant that much capital.",
            split_considered=True,
            split_why="Independent failure modes were compared before selecting one name.",
            sizing_why="The funded amount is survivable and changes portfolio outcomes.",
        ) for index, buy in enumerate(buys, start=1)],
        no_action=[NoActionLine(
            symbol="SCHD",
            reason="Deliberately unchanged in this funding run.",
            voices=[VoiceVerdict(
                source="holdings_review", verdict="HOLD", as_of=now,
                rationale="current run",
            )],
        )],
        rationale="Fills the largest NVDA-decorrelated plan gaps.",
    )
    assert validate_order_sheet(sheet).valid
    return SimpleNamespace(
        authored=SimpleNamespace(status="accepted"),
        order_sheet=SimpleNamespace(status="validated", sheet=sheet, failures=[]),
    )


def _accepted_switch() -> SimpleNamespace:
    now = datetime.now(UTC)
    evidence = MarketEvidence(
        price_usd=10,
        price_as_of=now,
        price_source="live-test-seam",
        market_cap_usd=2_000_000_000,
        market_cap_as_of=now,
        market_cap_source="live-test-seam",
        incorporation_country="United States",
        incorporation_as_of=now,
        incorporation_source="issuer",
    )

    def _intent_fields(symbol: str) -> dict:
        return {
            "thesis": f"Current evidence supports the {symbol} disposition.",
            "thesis_type": ThesisType.CONVEXITY,
            "falsifier": "The dated evidence invalidates the thesis.",
            "catalyst": DatedCatalyst(
                description="Scheduled thesis checkpoint",
                due_date=date(2027, 1, 31),
            ),
            "expectation": OutcomeExpectation(
                statement="The funded switch improves risk-adjusted outcomes.",
                due_date=date(2027, 2, 28),
                success_measure="switch outperforms its retained alternative",
            ),
            "evidence": evidence,
            "post_trade_weight_pct": 1.2,
            "expected_upside_multiple": 5,
            "outcome_scenarios": [
                OutcomeScenario(label="wipeout", probability_pct=30, terminal_multiple=0.1, terminal_date=date(2030, 8, 26), rationale="Thesis fails."),
                OutcomeScenario(label="base", probability_pct=50, terminal_multiple=1, terminal_date=date(2030, 8, 26), rationale="Mixed outcome."),
                OutcomeScenario(label="upside", probability_pct=20, terminal_multiple=5, terminal_date=date(2030, 8, 26), rationale="Thesis succeeds."),
            ],
            "probability_confidence": "LOW",
            "probability_basis": "Clinical base rates and current evidence.",
            "probability_weighted_multiple": 1.53,
            "expected_portfolio_contribution_pct": 0.1,
            "capital_at_risk_pct": 1.2,
            "estate_situs": "US",
            "constraint_costs": [ConstraintCost(
                constraint="us_estate_tax_exposure",
                estimated_cost_usd=4_000,
                basis_value_usd=10_000,
                valuation_basis="estimated_fmv_at_event",
                as_of=now,
                method="Test projection from estimated FMV at event.",
            )],
        }

    sheet = OrderSheet(
        user_id="ariel",
        generated_at=now,
        horizon_years_min=1,
        horizon_years_max=5,
        portfolio_symbols=["NVDA", "SCHD"],
        funding=FundingSummary(
            new_cash_usd=0,
            gross_sell_proceeds_usd=35_000,
            sell_tax_usd=8_750,
            sell_costs_usd=250,
            reserve_usd=0,
            available_to_buy_usd=26_000,
        ),
        lines=[
            OrderLine(
                symbol="GLUE",
                action=OrderAction.BUY,
                shares=2_600,
                notional_usd=26_000,
                venue="NASDAQ",
                stance_source="discovery",
                voices=[VoiceVerdict(
                    source="deployment_author",
                    verdict="BUY",
                    as_of=now,
                    rationale="Unified current-run decision.",
                )],
                **_intent_fields("GLUE"),
            ),
            OrderLine(
                symbol="NVDA",
                action=OrderAction.TRIM,
                shares=3_500,
                notional_usd=35_000,
                venue="NASDAQ",
                stance_source="funding_switch",
                voices=[VoiceVerdict(
                    source="deployment_author",
                    verdict="TRIM",
                    as_of=now,
                    rationale="Unified current-run funding decision.",
                )],
                tax=TaxImpact(
                    gross_proceeds_usd=35_000,
                    cost_basis_usd=0,
                    taxable_gain_usd=35_000,
                    effective_tax_rate=0.25,
                    estimated_tax_usd=8_750,
                    net_proceeds_usd=26_250,
                    method="Israeli CGT test seam",
                    authoritative=True,
                    as_of=now,
                ),
                **_intent_fields("NVDA"),
            ),
        ],
        candidate_comparisons=[CandidateComparison(
            ticker="GLUE",
            selection="SELECTED",
            radar_rank=1,
            radar_score=80.0,
            research_verdict="BUY",
            research_conviction="MED",
            evidence_fresh_as_of=now,
            key_advantage="Best comparative convexity in the funded sleeve.",
            key_risk="The dated thesis can fail.",
            why="Selected by the authored comparison for this test run.",
            outcome_scenarios=[
                OutcomeScenario(label="wipeout", probability_pct=30, terminal_multiple=0.1, terminal_date=date(2030, 8, 26), rationale="Thesis fails."),
                OutcomeScenario(label="base", probability_pct=50, terminal_multiple=1, terminal_date=date(2030, 8, 26), rationale="Mixed outcome."),
                OutcomeScenario(label="upside", probability_pct=20, terminal_multiple=5, terminal_date=date(2030, 8, 26), rationale="Thesis succeeds."),
            ],
            probability_confidence="LOW",
            probability_basis="Equal-basis industry rates and current evidence.",
            recommended_position_usd=26_000,
            smaller_position_usd=13_000,
            why_not_smaller="The upside would not materially affect the portfolio.",
            larger_position_usd=52_000,
            why_not_larger="The evidence does not warrant that much capital.",
            split_considered=True,
            split_why="Independent failure modes were compared before selecting one name.",
            sizing_why="The funded amount is survivable and changes portfolio outcomes.",
        )],
        no_action=[NoActionLine(
            symbol="SCHD",
            reason="Deliberately unchanged in the unified run.",
            voices=[VoiceVerdict(
                source="holdings_review",
                verdict="HOLD",
                as_of=now,
                rationale="Current run retained the position.",
            )],
        )],
        rationale="One after-tax sell-funded order sheet.",
    )
    assert validate_order_sheet(sheet).valid
    return SimpleNamespace(
        authored=SimpleNamespace(status="accepted"),
        order_sheet=SimpleNamespace(status="validated", sheet=sheet, failures=[]),
    )


def _open_directives(s: Session) -> list:
    return s.execute(sa.text(
        "SELECT id, status, summary, suggested_payload FROM action_proposals "
        "WHERE kind='allocate' AND dedup_key LIKE 'period_directive:%' "
        "ORDER BY id"
    )).fetchall()


def test_compose_calls_canonical_live_order_sheet_path(monkeypatch):
    captured: dict = {}
    sentinel = object()

    def _deploy(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("argosy.api.routes.portfolio.get_deploy_cash", _deploy)
    db = object()
    assert compose_authored_directive(db, user_id="ariel", excess_usd=120_000) is sentinel
    assert captured == {
        "cash_usd": 120_000.0,
        "user_id": "ariel",
        "live": True,
        "sleeve_pct": 5.0,
        "use_high_potential": True,
        "fleet_review": False,
        "include_order_sheet": True,
        "allow_sells": True,
        "horizon_years_min": 1,
        "horizon_years_max": 5,
        "db": db,
    }


# --------------------------------------------------------------------------
# Triage
# --------------------------------------------------------------------------


def test_triage_skip_below_threshold_is_a_quiet_success(alembic_engine_at_head):
    """No cash overage → no LLM, no proposal, triggered=False (a skipped day is
    a SUCCESS state, not an error)."""
    def _no_compose(db, *, user_id, excess_usd):  # pragma: no cover — must not fire
        raise AssertionError("compose must not run when triage says quiet")

    with Session(alembic_engine_at_head) as s:
        out = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: None, compose_fn=_no_compose,
        )
    assert out["triggered"] is False
    assert "below plan-target threshold" in out["reason"]
    assert out["proposal_id"] is None and out["superseded"] == []


def test_triage_skip_when_open_directive_still_accurate(alembic_engine_at_head):
    """An open directive whose cash figure is within ±10% of today's → nothing
    new to say; the fleet never fires and the existing row keeps the slot."""
    compose_calls: list[float] = []

    def _compose(db, *, user_id, excess_usd):
        compose_calls.append(excess_usd)
        return _accepted(Buy(symbol="EXUS", amount_usd=excess_usd))

    with Session(alembic_engine_at_head) as s:
        # Day 1: trigger → compose → sink.
        out1 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(100_000.0),
            compose_fn=_compose,
        )
        assert out1["triggered"] is True and out1["proposal_id"] is not None
        # Day 2: cash drifted 5% — inside the band → quiet skip, no compose.
        out2 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(105_000.0),
            compose_fn=_compose,
        )
    assert out2["triggered"] is False
    assert "still accurate" in out2["reason"]
    assert out2["proposal_id"] == out1["proposal_id"]
    assert compose_calls == [100_000.0]  # the fleet fired exactly once


def test_legacy_prose_directive_is_upgraded_even_when_cash_is_unchanged(
    alembic_engine_at_head,
):
    compose_calls: list[float] = []

    def _compose(db, *, user_id, excess_usd):
        compose_calls.append(excess_usd)
        return _accepted(Buy(symbol="EXUS", amount_usd=excess_usd))

    with Session(alembic_engine_at_head) as s:
        first = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(100_000.0),
            compose_fn=_compose,
        )
        s.execute(
            sa.text("UPDATE action_proposals SET suggested_payload=:p WHERE id=:i"),
            {"i": first["proposal_id"], "p": json.dumps({"excess_usd": 100_000.0})},
        )
        s.commit()
        second = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(105_000.0),
            compose_fn=_compose,
        )
        payload = json.loads(_open_directives(s)[0][3])

    assert second["triggered"] is True
    assert second["proposal_id"] == first["proposal_id"]
    assert compose_calls == [100_000.0, 105_000.0]
    assert payload["artifact_type"] == "validated_order_sheet"


def test_current_recommendations_trigger_one_sell_funded_sheet_without_idle_cash(
    alembic_engine_at_head,
):
    compose_calls: list[float] = []

    def _compose(db, *, user_id, excess_usd):
        compose_calls.append(excess_usd)
        return _accepted_switch()

    with Session(alembic_engine_at_head) as s:
        now = datetime.now(UTC)
        source_rows = [
            Proposal(
                user_id="ariel", ticker="NVDA", action="sell",
                size_shares_or_currency=519, size_units="shares", tier="T2",
                status="awaiting_human", source="verdict_trigger_sweep", shadow=0,
                rationale_summary="Fund the better current opportunity.",
                expected_impact_json="{}", created_at=now, updated_at=now,
                expires_at=now + timedelta(days=2),
            ),
            Proposal(
                user_id="ariel", ticker="GLUE", action="buy",
                size_shares_or_currency=26_000, size_units="currency", tier="T2",
                status="awaiting_human", source="decision_funnel", shadow=0,
                rationale_summary="Current discovery recommendation.",
                expected_impact_json="{}", created_at=now, updated_at=now,
                expires_at=now + timedelta(days=2),
            ),
        ]
        s.add_all(source_rows)
        s.commit()
        source_ids = sorted(int(row.id) for row in source_rows)
        first = run_period_directive_daily(
            s,
            "ariel",
            detect_fn=lambda db, *, user_id: None,
            compose_fn=_compose,
        )
        second = run_period_directive_daily(
            s,
            "ariel",
            detect_fn=lambda db, *, user_id: None,
            compose_fn=_compose,
        )
        rows = _open_directives(s)
        statuses = dict(s.query(Proposal.id, Proposal.status).filter(
            Proposal.id.in_(source_ids),
        ).all())
        histories = s.query(ProposalHistory).filter(
            ProposalHistory.proposal_id.in_(source_ids),
            ProposalHistory.transitioned_by == "period_directive_daily",
        ).all()

    assert first["triggered"] is True
    assert first["cash_usd"] == 0
    assert first["source_proposal_ids"] == source_ids
    assert first["consumed_source_proposal_ids"] == source_ids
    assert first["materialization_status"] == "awaiting_unified_approval"
    payload = json.loads(rows[0][3])
    assert payload["source_proposal_ids"] == source_ids
    assert payload["order_sheet"]["funding"] == {
        "available_to_buy_usd": 26_000.0,
        "gross_sell_proceeds_usd": 35_000.0,
            "new_cash_usd": 0.0,
            "reserve_usd": 0.0,
            "rounding_residual_usd": 0.0,
            "sell_costs_usd": 250.0,
        "sell_tax_usd": 8_750.0,
    }
    assert second["triggered"] is False
    assert second["source_proposal_ids"] == source_ids
    assert statuses == {proposal_id: "cancelled" for proposal_id in source_ids}
    assert len(histories) == 2
    assert compose_calls == [0.0]


def test_disputed_holding_review_triggers_unified_reconciliation_without_idle_cash(
    alembic_engine_at_head,
):
    compose_calls: list[float] = []

    def _compose(db, *, user_id, excess_usd):
        compose_calls.append(excess_usd)
        return _accepted_switch()

    with Session(alembic_engine_at_head) as s:
        review = HoldingReview(
            user_id="ariel", symbol="TSLA", reviewed_at=datetime.now(UTC),
            verdict="TRIM", confidence="MED", reason="fresh earnings weakened",
            evidence_json="{}", position_usd=10_000, elevated_by_flag=True,
            outcome="held_unverified",
        )
        s.add(review)
        s.commit()
        first = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: None,
            compose_fn=_compose,
        )
        payload = json.loads(_open_directives(s)[0][3])
        second = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: None,
            compose_fn=_compose,
        )

    key = f"holding_review:{review.id}"
    assert first["triggered"] is True
    assert first["source_proposal_ids"] == []
    assert first["source_recommendation_keys"] == [key]
    assert payload["source_recommendation_keys"] == [key]
    assert second["triggered"] is False
    assert compose_calls == [0.0]


# --------------------------------------------------------------------------
# Compose → sink happy path
# --------------------------------------------------------------------------


def test_trigger_compose_sink_happy_path(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as s:
        out = run_period_directive_daily(
            s, "ariel",
            detect_fn=lambda db, *, user_id: _event(171_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: _accepted(
                Buy(symbol="EXUS", amount_usd=80_000.0, sleeve="Ex-US developed"),
                Buy(symbol="FUSA", amount_usd=60_000.0, sleeve="US quality"),
                Buy(symbol="DPYA", amount_usd=31_000.0, sleeve="EM dividend"),
            ),
        )
        rows = _open_directives(s)
    assert out["triggered"] is True and out["cash_usd"] == 171_000.0
    assert out["proposal_id"] == rows[0][0]
    assert len(rows) == 1 and rows[0][1] == "open"
    summary = rows[0][2]
    assert summary.startswith("Validated unified order sheet for ~$171k idle cash:")
    assert "EXUS $80k" in summary and "FUSA $60k" in summary and "DPYA $31k" in summary
    payload = json.loads(rows[0][3])
    assert payload["excess_usd"] == 171_000.0
    assert payload["artifact_type"] == "validated_order_sheet"
    assert payload["validation_status"] == "validated"
    assert payload["order_sheet"]["funding"]["available_to_buy_usd"] == 171_000.0
    assert [b["symbol"] for b in payload["buys"]] == ["EXUS", "FUSA", "DPYA"]
    assert out["materialization_status"] == "awaiting_unified_approval"


def test_degraded_author_writes_nothing(alembic_engine_at_head):
    """Author unavailable → degraded summary, NO proposal, NO deterministic
    fallback allocation — fail quiet-but-logged, retry next day."""
    with Session(alembic_engine_at_head) as s:
        out = run_period_directive_daily(
            s, "ariel",
            detect_fn=lambda db, *, user_id: _event(150_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: SimpleNamespace(
                authored=SimpleNamespace(status="unavailable"),
                order_sheet=SimpleNamespace(
                    status="unavailable", sheet=None, failures=["reviewer unavailable"]
                ),
            ),
        )
        assert _open_directives(s) == []
    assert out["triggered"] is True and out.get("degraded") is True
    assert out["proposal_id"] is None
    assert "validated order sheet unavailable" in out["reason"]
    assert out["status"] == "degraded"


def test_degraded_replacement_supersedes_a_now_invalid_current_sheet(
    alembic_engine_at_head,
):
    """An invalid old sheet must not linger as the UI's blocked current plan."""
    with Session(alembic_engine_at_head) as s:
        first = run_period_directive_daily(
            s,
            "ariel",
            detect_fn=lambda db, *, user_id: _event(100_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: _accepted(
                Buy(symbol="EXUS", amount_usd=excess_usd)
            ),
        )
        row = s.get(ActionProposal, first["proposal_id"])
        payload = json.loads(row.suggested_payload)
        payload["order_sheet"]["funding"]["available_to_buy_usd"] = 1.0
        row.suggested_payload = json.dumps(payload)
        s.commit()

        degraded = run_period_directive_daily(
            s,
            "ariel",
            detect_fn=lambda db, *, user_id: _event(100_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: SimpleNamespace(
                authored=SimpleNamespace(status="rejected"),
                order_sheet=SimpleNamespace(
                    status="invalid", sheet=None, failures=["review disagreement"]
                ),
            ),
        )
        status = s.get(ActionProposal, first["proposal_id"]).status

    assert degraded["status"] == "degraded"
    assert degraded["superseded"] == [first["proposal_id"]]
    assert status == "superseded"


def test_configured_account_waits_for_unified_approval(alembic_engine_at_head):
    captured: dict = {}

    def _materialize(db, sheet, *, funding_account_id, sell_accounts_by_symbol):
        captured.update(
            sheet=sheet,
            funding_account_id=funding_account_id,
            sell_accounts_by_symbol=sell_accounts_by_symbol,
        )
        return [SimpleNamespace(id=901), SimpleNamespace(id=902)]

    with Session(alembic_engine_at_head) as s:
        out = run_period_directive_daily(
            s,
            "ariel",
            detect_fn=lambda db, *, user_id: _event(120_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: _accepted(
                Buy(symbol="EXUS", amount_usd=80_000.0),
                Buy(symbol="CMPS", amount_usd=40_000.0),
            ),
            funding_account_id="schwab_rsu",
            materialize_fn=_materialize,
        )

    assert out["materialization_status"] == "awaiting_unified_approval"
    assert out["materialized_proposal_ids"] == []
    assert captured == {}


# --------------------------------------------------------------------------
# Supersede / refresh-in-place
# --------------------------------------------------------------------------


def test_stale_directive_is_refreshed_in_place(alembic_engine_at_head):
    """Cash moved >10% → re-author; the dedup collision refreshes the OPEN row
    in place (same id) so the inbox never shows a stale amount."""
    with Session(alembic_engine_at_head) as s:
        out1 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(100_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: _accepted(
                Buy(symbol="EXUS", amount_usd=excess_usd)),
        )
        out2 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(150_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: _accepted(
                Buy(symbol="DPYA", amount_usd=excess_usd)),
        )
        rows = _open_directives(s)
    assert out2["triggered"] is True
    assert out2["proposal_id"] == out1["proposal_id"]  # same slot, refreshed
    assert len(rows) == 1
    assert "$150k" in rows[0][2] and "DPYA" in rows[0][2]
    assert "$100k" not in rows[0][2]


def test_open_directive_superseded_when_cash_falls_below_threshold(
    alembic_engine_at_head,
):
    """The cash got deployed / dropped under threshold → the standing directive
    is moot and must DISAPPEAR from the client's checklist (auto-supersede)."""
    with Session(alembic_engine_at_head) as s:
        out1 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: _event(120_000.0),
            compose_fn=lambda db, *, user_id, excess_usd: _accepted(
                Buy(symbol="EXUS", amount_usd=excess_usd)),
        )
        out2 = run_period_directive_daily(
            s, "ariel", detect_fn=lambda db, *, user_id: None,
            compose_fn=lambda db, *, user_id, excess_usd: None,
        )
        row = s.execute(sa.text(
            "SELECT status FROM action_proposals WHERE id = :i"
        ), {"i": out1["proposal_id"]}).fetchone()
    assert out2["triggered"] is False
    assert out2["superseded"] == [out1["proposal_id"]]
    assert row[0] == "superseded"


# --------------------------------------------------------------------------
# Scheduler contract
# --------------------------------------------------------------------------


def test_tick_accepts_the_scheduler_clock_keyword() -> None:
    """The scheduler calls every loop as tick(now=self.clock) — the keyword MUST
    be accepted (the pending_reevaluation_daily regression)."""
    captured = {}

    def _run(session, user_id):
        captured["user_id"] = user_id
        return {"triggered": False, "reason": "stub", "cash_usd": None,
                "proposal_id": None, "superseded": []}

    class _FakeSession:
        def close(self): pass

    job = PeriodDirectiveDailyJob(
        enabled=True, user_id="ariel",
        session_factory=lambda: _FakeSession(), run_fn=_run,
    )
    clock = lambda: datetime(2026, 7, 6, 16, 0, tzinfo=UTC)  # noqa: E731
    out = asyncio.run(job.tick(now=clock))
    assert captured["user_id"] == "ariel"
    assert out["triggered"] is False
    assert job.last_output_summary == out


def test_period_directive_daily_registers_with_its_own_metadata() -> None:
    """Pin the real registration pair main.py uses: a CadenceLoop MUST register
    with long_running=False (the LongRunningJob-type discriminator — True makes
    JobRegistry.register raise and leaves the loop unrunnable)."""
    from argosy.services.jobs.registry import JobRegistry

    reg = JobRegistry()
    reg.register(job=PeriodDirectiveDailyJob(enabled=True, user_id="ariel"),
                 metadata=period_directive_daily_metadata())
    assert "period_directive_daily" in reg._jobs  # type: ignore[attr-defined]
    md = period_directive_daily_metadata()
    assert md.schedule_cron == "0 19 * * *" and md.long_running is False
