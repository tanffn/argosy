from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.services.order_sheet import (
    FundingSummary,
    OrderSheet,
    validate_order_sheet,
)
from argosy.services.order_sheet_state import (
    build_no_action_lines,
    load_portfolio_voices,
)
from argosy.state.models import Base, PositionStance, User

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(User(id="ariel"))
    session.commit()
    return session


def test_schd_sell_vs_hold_is_preserved_as_prior_self_audit_context() -> None:
    db = _session()
    db.add(
        PositionStance(
            user_id="ariel",
            symbol="SCHD",
            stance="SELL",
            stance_source="plan",
            conviction="HIGH",
            plan_verdict="SELL",
            review_verdict="HOLD",
            review_outcome="hold",
            divergence=True,
            reasoning_md="plan and review disagree",
            built_at=NOW,
        )
    )
    db.commit()
    voices = load_portfolio_voices(
        db,
        user_id="ariel",
        portfolio_symbols={"SCHD"},
        observed_at=NOW,
    )
    no_action = build_no_action_lines(
        holdings_usd={"SCHD": 50_000},
        acted_symbols=set(),
        voices_by_symbol=voices,
    )
    sheet = OrderSheet(
        user_id="ariel",
        generated_at=NOW,
        horizon_years_min=1,
        horizon_years_max=5,
        portfolio_symbols=["SCHD"],
        funding=FundingSummary(
            new_cash_usd=0,
            gross_sell_proceeds_usd=0,
            sell_tax_usd=0,
            sell_costs_usd=0,
            reserve_usd=0,
            available_to_buy_usd=0,
        ),
        lines=[],
        no_action=no_action,
        rationale="test",
    )
    codes = {f.code for f in validate_order_sheet(sheet).failures}
    assert "one_voice_conflict" not in codes
    assert "no_action_contradicts_voice" not in codes
    assert {v.decision_scope for v in no_action[0].voices} == {"prior_context"}
    assert "retained for self-audit" in no_action[0].reason


def test_missing_stance_is_not_minted_as_a_trusted_hold() -> None:
    db = _session()
    voices = load_portfolio_voices(
        db,
        user_id="ariel",
        portfolio_symbols={"UNKNOWN"},
        observed_at=NOW,
    )
    assert voices["UNKNOWN"][0].source == "coverage_missing"
