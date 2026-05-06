"""Routes accepted speculative candidates from `current` -> Argonaut paper queue.

Per SDD §10.1: T0 routing in the limited account auto-executes when in
`live`; in `paper` it logs a PaperFill; otherwise it queues for human
single-click.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import PlanVersion, User


@pytest.fixture
def session_with_current(alembic_engine_at_head):
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.add(PlanVersion(
        user_id="ariel", role="current", version_label="synth-x", raw_markdown="",
        horizon_long_json='{"horizon":"long","freshness_expected":"annual","status":"no_change","posture":"x"}',
        horizon_medium_json='{"horizon":"medium","freshness_expected":"quarterly","status":"no_change","posture":"x"}',
        horizon_short_json=(
            '{"horizon":"short","freshness_expected":"monthly","status":"no_change",'
            '"posture":"x","speculative_candidates":['
            '{"ticker":"HOOD","thesis_summary":"momentum",'
            '"suggested_position_usd":800,"suggested_position_pct_of_net_worth":0.0008,'
            '"risk_ceiling_check":true,"horizon_days":30,"expected_drawdown_pct":0.2,'
            '"exit_trigger":"stop -20%, take +50%","sourced_from":["sentiment"]}'
            ']}'
        ),
    ))
    s.commit()
    yield s
    s.close()


def test_route_speculative_creates_proposal_in_argonaut_paper(session_with_current, monkeypatch):
    from argosy.orchestrator import speculation_router as router

    routed: list[dict] = []
    def _fake_create_proposal(**kw):
        routed.append(kw)
        return type("P", (), {"id": 999})()

    monkeypatch.setattr(router, "_create_proposal", _fake_create_proposal)

    out = router.route_accepted_candidate(
        session_with_current,
        user_id="ariel",
        ticker="HOOD",
        execution_mode="paper",
    )
    assert out.proposal_id == 999
    assert routed[0]["ticker"] == "HOOD"
    assert routed[0]["account_class"] == "argonaut"
    assert routed[0]["tier"] == "T0"
    assert routed[0]["paper"] is True


def test_route_speculative_rejects_unknown_ticker(session_with_current):
    from argosy.orchestrator import speculation_router as router

    with pytest.raises(router.UnknownCandidateError):
        router.route_accepted_candidate(
            session_with_current, user_id="ariel", ticker="NOPE", execution_mode="paper",
        )


def test_route_speculative_blocks_when_cap_breached(session_with_current, monkeypatch):
    """Defense-in-depth: even if the synthesizer somehow emitted an over-cap
    candidate, the router refuses to act on it.
    """
    # Forcibly mutate the candidate to be over cap.
    sess = session_with_current
    pv = sess.query(PlanVersion).filter_by(user_id="ariel", role="current").one()
    import json
    short = json.loads(pv.horizon_short_json)
    short["speculative_candidates"][0]["suggested_position_pct_of_net_worth"] = 0.10  # 10% NW
    pv.horizon_short_json = json.dumps(short)
    sess.commit()

    from argosy.orchestrator import speculation_router as router
    with pytest.raises(router.CapBreachError):
        router.route_accepted_candidate(
            sess, user_id="ariel", ticker="HOOD", execution_mode="paper",
        )


def test_route_speculative_blocks_when_allowed_account_classes_empty(
    session_with_current, monkeypatch,
):
    """Wave 3 spec-compliance fix: an empty ``allowed_account_classes``
    means speculation is disabled — the router refuses to route.
    """
    from argosy.orchestrator import speculation_router as router
    from argosy.config import SpeculationCap

    monkeypatch.setattr(
        router, "load_speculation_cap",
        lambda **_kw: SpeculationCap(allowed_account_classes=()),
    )
    monkeypatch.setattr(router, "get_user_agent_settings", lambda _uid: {})

    with pytest.raises(router.CapBreachError, match="allowed_account_classes"):
        router.route_accepted_candidate(
            session_with_current,
            user_id="ariel", ticker="HOOD", execution_mode="paper",
        )


def test_route_speculative_uses_first_allowed_account_class(
    session_with_current, monkeypatch,
):
    """Wave 3 spec-compliance fix: the router routes to the first entry
    of ``allowed_account_classes`` rather than hardcoding ``argonaut``.
    """
    from argosy.orchestrator import speculation_router as router
    from argosy.config import SpeculationCap

    captured: list[dict] = []
    monkeypatch.setattr(
        router, "_create_proposal",
        lambda **kw: (captured.append(kw) or type("P", (), {"id": 7})()),
    )
    monkeypatch.setattr(
        router, "load_speculation_cap",
        lambda **_kw: SpeculationCap(allowed_account_classes=("limited",)),
    )
    monkeypatch.setattr(router, "get_user_agent_settings", lambda _uid: {})

    out = router.route_accepted_candidate(
        session_with_current,
        user_id="ariel", ticker="HOOD", execution_mode="paper",
    )
    assert out.proposal_id == 7
    assert captured[0]["account_class"] == "limited"
