from datetime import UTC, datetime
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.services.order_sheet import (
    FundingSummary,
    OrderSheet,
    VoiceVerdict,
    validate_order_sheet,
)
from argosy.services.order_sheet_state import (
    build_no_action_lines,
    load_portfolio_voices,
)
from argosy.state.models import Base, HoldingReview, PositionStance, User

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("label", ["WAIT", "WATCH", "DEFER_PENDING_EVIDENCE"])
def test_prior_labels_survive_without_becoming_current_actions(label):
    kwargs = dict(source="plan:old", verdict=label, as_of=NOW, rationale="Original decision")
    prior = VoiceVerdict(**kwargs, decision_scope="prior_context")
    assert prior.verdict == label
    assert VoiceVerdict.model_validate_json(prior.model_dump_json()).verdict == label
    with pytest.raises(ValueError, match="Current-run verdict"):
        VoiceVerdict(**kwargs)


def test_saved_wait_and_hold_load_verbatim_without_aliasing_or_erasing():
    db = _session()
    db.add(PositionStance(user_id="ariel", symbol="DNN", stance="WAIT",
                          stance_source="plan", conviction="LOW", plan_verdict="WAIT",
                          review_verdict="HOLD", review_outcome="hold", built_at=NOW))
    db.commit()
    voices = load_portfolio_voices(db, user_id="ariel", portfolio_symbols={"DNN"})
    assert [v.verdict for v in voices["DNN"]] == ["WAIT", "HOLD"]
    lines = build_no_action_lines(holdings_usd={"DNN": 1000}, acted_symbols=set(), voices_by_symbol=voices)
    assert "WAIT" in lines[0].reason and "HOLD" in lines[0].reason
    assert db.query(PositionStance).one().plan_verdict == "WAIT"


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


def test_saved_review_reaches_sheet_without_portfolio_cache_refresh() -> None:
    db = _session()
    db.add(HoldingReview(user_id="ariel", symbol="NEW", reviewed_at=NOW,
                         verdict="HOLD", outcome="hold", reason="Current thesis intact."))
    db.commit()
    assert db.query(PositionStance).count() == 0
    voices = load_portfolio_voices(db, user_id="ariel", portfolio_symbols={"NEW"})
    assert [(v.source, v.verdict, v.as_of, v.rationale) for v in voices["NEW"]] == [
        ("review:hold", "HOLD", NOW, "Current thesis intact.")]
    assert db.query(PositionStance).count() == 0  # loader never invents a plan stance


def test_latest_saved_review_replaces_cached_review_but_preserves_plan() -> None:
    from datetime import timedelta
    db = _session()
    db.add(PositionStance(user_id="ariel", symbol="NEW", stance="SELL",
                          stance_source="plan", conviction="MED", plan_verdict="SELL", review_verdict="SELL",
                          review_outcome="proposed", built_at=NOW - timedelta(days=1)))
    db.add(HoldingReview(user_id="ariel", symbol="NEW", reviewed_at=NOW,
                         verdict="HOLD", outcome="hold", reason="New earnings support holding."))
    db.commit()
    voices = load_portfolio_voices(db, user_id="ariel", portfolio_symbols={"NEW"})["NEW"]
    assert [(v.source.split(":")[0], v.verdict) for v in voices] == [
        ("plan", "SELL"), ("review", "HOLD")]
    assert voices[1].as_of == NOW


def test_disputed_or_other_tenant_review_does_not_fill_missing_coverage() -> None:
    from datetime import timedelta
    db = _session()
    db.add(User(id="other"))
    db.commit()
    db.add(PositionStance(user_id="ariel", symbol="DISPUTED", stance="HOLD",
                          stance_source="review", conviction="MED", review_verdict="HOLD",
                          review_outcome="hold", built_at=NOW - timedelta(days=1)))
    db.add_all([
        HoldingReview(user_id="other", symbol="NEW", reviewed_at=NOW,
                      verdict="HOLD", outcome="hold", reason="Other tenant."),
        HoldingReview(user_id="ariel", symbol="DISPUTED", reviewed_at=NOW - timedelta(days=1),
                      verdict="HOLD", outcome="hold", reason="Earlier opinion."),
        HoldingReview(user_id="ariel", symbol="DISPUTED", reviewed_at=NOW,
                      verdict="SELL", outcome="held_unverified", reason="Disputed new evidence."),
    ])
    db.commit()
    voices = load_portfolio_voices(db, user_id="ariel", portfolio_symbols={"NEW", "DISPUTED"})
    assert all(v[0].source == "coverage_missing" for v in voices.values())
