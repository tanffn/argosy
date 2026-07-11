"""API / loop wiring tests for the verdict pushback gate (Item B)."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import ActionProposal, User


@pytest.fixture
def session(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


@pytest.mark.asyncio
async def test_run_decision_flow_defends_without_agent_spawn(session, monkeypatch):
    """Synthetic pushback without new facts → status=defended, zero flow.run calls."""
    from argosy.api.routes import decisions as decisions_mod
    from argosy.services.verdict_registry import write_verdict

    write_verdict(
        session, user_id="ariel", subject="NOW",
        verdict="SELL", conviction="HIGH",
        falsifiers=["GAAP profitability"],
        source_decision_run_id=164,
    )
    session.commit()

    # Point the route's sync engine at the test DB.
    monkeypatch.setattr(
        "argosy.state.db.get_engine",
        lambda: session.get_bind(),
    )

    ran = {"n": 0}

    async def _boom(*a, **k):
        ran["n"] += 1
        raise AssertionError("DecisionFlow.run must not be called when DEFENDED")

    monkeypatch.setattr(
        decisions_mod.DecisionFlow, "run", _boom,
    )
    monkeypatch.setattr(
        decisions_mod, "load_agent_settings",
        lambda user_id: type("S", (), {"tiers": type("T", (), {"cooling_off_hours_t3": 24})()})(),
    )

    body = decisions_mod.RunRequest(
        user_id="ariel", ticker="NOW", tier="T2",
        cited_new_facts=["Ariel thinks x2-3"],
    )
    resp = await decisions_mod.run_decision_flow(body)
    assert resp.status == "defended"
    assert resp.blocked_by == "verdict_defended"
    assert resp.standing_verdict is not None
    assert resp.standing_verdict["verdict"] == "SELL"
    assert ran["n"] == 0


@pytest.mark.asyncio
async def test_verdict_trigger_loop_writes_unlock_row(session):
    from argosy.orchestrator.loops.verdict_trigger_daily import VerdictTriggerDailyLoop
    from argosy.services.verdict_registry import write_verdict

    write_verdict(
        session, user_id="ariel", subject="ORCL",
        verdict="WAIT", conviction="HIGH",
        revisit_triggers=[{"kind": "price_below", "price": 115.0}],
        source_decision_run_id=198,
    )
    session.commit()

    def _quotes(sess, user_id, subjects):
        return {"ORCL": 110.0}

    SessionLocal = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    loop = VerdictTriggerDailyLoop(
        enabled=True,
        user_id="ariel",
        session_factory=SessionLocal,
        quotes_fn=_quotes,
        today=date(2026, 7, 11),
    )
    out = await loop.tick()
    assert out.get("fired") == 1
    assert out.get("unlock_proposal_ids")
    prop = session.get(ActionProposal, out["unlock_proposal_ids"][0])
    # Refresh from DB via new query.
    SessionLocal2 = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    s2 = SessionLocal2()
    try:
        prop = s2.get(ActionProposal, out["unlock_proposal_ids"][0])
        assert prop is not None
        assert "revisit unlocked: ORCL" in prop.summary
    finally:
        s2.close()
