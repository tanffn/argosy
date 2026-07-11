"""Retract-on-reversal unit tests (Item C)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Proposal, ProposalHistory, User


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


def _open_proposal(
    session,
    *,
    ticker: str,
    action: str,
    decision_run_id: int | None = None,
    shadow: int = 0,
    status: str = "awaiting_human",
) -> Proposal:
    row = Proposal(
        user_id="ariel",
        ticker=ticker.upper(),
        action=action,
        size_shares_or_currency=10,
        size_units="shares",
        instrument="stock",
        order_type="market",
        time_in_force="DAY",
        tier="T2",
        account_class="main",
        status=status,
        rationale_summary=f"test {action} {ticker}",
        expected_impact_json="{}",
        decision_run_id=decision_run_id,
        shadow=shadow,
    )
    session.add(row)
    session.flush()
    return row


def test_hold_retracts_open_sell_atomically_with_history_note(session):
    from argosy.decisions.retract_on_reversal import (
        RETRACT_TRANSITIONED_BY,
        history_note_for_retract,
        retract_contradictory_open_proposals,
    )
    from argosy.services.verdict_registry import write_verdict

    sell = _open_proposal(session, ticker="CRM", action="sell", decision_run_id=100)
    session.commit()

    # Same transaction: verdict write + retract (Item C atomic unit).
    write_verdict(
        session,
        user_id="ariel",
        subject="CRM",
        verdict="HOLD",
        conviction="MED",
        source_decision_run_id=167,
        reasoning_md="keep CRM as a bounded, trigger-monitored slot",
    )
    cancelled = retract_contradictory_open_proposals(
        session,
        user_id="ariel",
        ticker="CRM",
        verdict="HOLD",
        decision_run_id=167,
        detail="keep CRM as a bounded, trigger-monitored slot",
    )
    session.commit()

    assert cancelled == [sell.id]
    session.refresh(sell)
    assert sell.status == "cancelled"
    hist = (
        session.query(ProposalHistory)
        .filter_by(proposal_id=sell.id, status="cancelled")
        .one()
    )
    assert hist.transitioned_by == RETRACT_TRANSITIONED_BY
    expected = history_note_for_retract(
        decision_run_id=167,
        verdict="HOLD",
        detail="keep CRM as a bounded, trigger-monitored slot",
    )
    assert hist.note == expected
    assert "re-adjudication run 167" in hist.note
    assert "One decision = one inbox row" in hist.note


def test_hold_retracts_open_buy_and_shadow(session):
    from argosy.decisions.retract_on_reversal import (
        retract_contradictory_open_proposals,
    )
    from argosy.services.verdict_registry import write_verdict

    buy = _open_proposal(session, ticker="NOW", action="buy", shadow=0)
    shadow_buy = _open_proposal(
        session, ticker="NOW", action="buy", shadow=1, status="cooling",
    )
    session.commit()

    write_verdict(
        session, user_id="ariel", subject="NOW", verdict="WAIT",
        conviction="HIGH", source_decision_run_id=200,
    )
    cancelled = retract_contradictory_open_proposals(
        session,
        user_id="ariel",
        ticker="NOW",
        verdict="WAIT",
        decision_run_id=200,
    )
    session.commit()

    assert set(cancelled) == {buy.id, shadow_buy.id}
    session.refresh(buy)
    session.refresh(shadow_buy)
    assert buy.status == "cancelled"
    assert shadow_buy.status == "cancelled"


def test_verdict_on_x_does_not_touch_y(session):
    from argosy.decisions.retract_on_reversal import (
        retract_contradictory_open_proposals,
    )
    from argosy.services.verdict_registry import write_verdict

    crm_sell = _open_proposal(session, ticker="CRM", action="sell")
    now_sell = _open_proposal(session, ticker="NOW", action="sell")
    session.commit()

    write_verdict(
        session, user_id="ariel", subject="CRM", verdict="HOLD",
        conviction="MED", source_decision_run_id=167,
    )
    cancelled = retract_contradictory_open_proposals(
        session,
        user_id="ariel",
        ticker="CRM",
        verdict="HOLD",
        decision_run_id=167,
    )
    session.commit()

    assert cancelled == [crm_sell.id]
    session.refresh(now_sell)
    assert now_sell.status == "awaiting_human"


def test_same_run_proposal_not_retracted(session):
    from argosy.decisions.retract_on_reversal import (
        retract_contradictory_open_proposals,
    )

    # Fresh BUY from this run must survive a BUY verdict write.
    fresh = _open_proposal(
        session, ticker="AAPL", action="buy", decision_run_id=300,
    )
    stale_sell = _open_proposal(
        session, ticker="AAPL", action="sell", decision_run_id=50,
    )
    session.commit()

    cancelled = retract_contradictory_open_proposals(
        session,
        user_id="ariel",
        ticker="AAPL",
        verdict="BUY",
        decision_run_id=300,
    )
    session.commit()
    assert cancelled == [stale_sell.id]
    session.refresh(fresh)
    assert fresh.status == "awaiting_human"


def test_buy_verdict_does_not_retract_unrelated_buy(session):
    from argosy.decisions.retract_on_reversal import (
        retract_contradictory_open_proposals,
    )

    other_buy = _open_proposal(session, ticker="MSFT", action="buy")
    session.commit()
    cancelled = retract_contradictory_open_proposals(
        session,
        user_id="ariel",
        ticker="AAPL",
        verdict="BUY",
        decision_run_id=301,
    )
    session.commit()
    assert cancelled == []
    session.refresh(other_buy)
    assert other_buy.status == "awaiting_human"
