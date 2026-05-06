"""Routes accepted speculative candidates from `current` -> Argonaut paper queue.

Per SDD §10.1: T0 routing in the limited account auto-executes when in
`live`; in `paper` it logs a PaperFill; otherwise it queues for human
single-click.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from argosy.state.models import DecisionRun, PlanVersion, User


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


def _fake_proposal(*, proposal_id: int = 999, account_class: str = "limited"):
    """Build a stub proposal compatible with the router's C2 sanity check.

    The router checks ``proposal.account_class`` after ``_create_proposal``
    returns (defense-in-depth against a future regression in the helper),
    so the stub must expose that attribute.
    """
    return type("P", (), {"id": proposal_id, "account_class": account_class})()


def test_route_speculative_creates_proposal_in_argonaut_paper(session_with_current, monkeypatch):
    from argosy.orchestrator import speculation_router as router

    routed: list[dict] = []
    def _fake_create_proposal(**kw):
        routed.append(kw)
        return _fake_proposal(proposal_id=999, account_class=kw["account_class"])

    monkeypatch.setattr(router, "_create_proposal", _fake_create_proposal)

    out = router.route_accepted_candidate(
        session_with_current,
        user_id="ariel",
        ticker="HOOD",
        execution_mode="paper",
    )
    assert out.proposal_id == 999
    assert routed[0]["ticker"] == "HOOD"
    # account_class must be "limited" — the DB/code string the broker
    # router checks (the *feature* "Argonaut" is the user-facing label
    # for that class).
    assert routed[0]["account_class"] == "limited"
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
    of ``allowed_account_classes`` rather than hardcoding the limited class.
    """
    from argosy.orchestrator import speculation_router as router
    from argosy.config import SpeculationCap

    captured: list[dict] = []

    def _capture(**kw):
        captured.append(kw)
        return _fake_proposal(proposal_id=7, account_class=kw["account_class"])

    monkeypatch.setattr(router, "_create_proposal", _capture)
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


def test_route_speculative_c2_defense_blocks_helper_account_class_drift(
    session_with_current, monkeypatch,
):
    """C2 defense-in-depth: if the helper returns a row whose
    ``account_class`` is *not* in ``cap.allowed_account_classes`` (e.g.
    a future regression in ``create_speculative_proposal`` flips a default
    arg), the router must raise ``CapBreachError`` rather than accept the
    drift.
    """
    from argosy.orchestrator import speculation_router as router

    # Helper "leaks" a wrong account_class compared to what we asked for.
    monkeypatch.setattr(
        router, "_create_proposal",
        lambda **_kw: _fake_proposal(proposal_id=11, account_class="main"),
    )

    with pytest.raises(router.CapBreachError, match="not in cap"):
        router.route_accepted_candidate(
            session_with_current,
            user_id="ariel", ticker="HOOD", execution_mode="paper",
        )


def test_route_speculative_threads_decision_run_id_for_audit_lineage(
    session_with_current, monkeypatch,
):
    """I1: the routed proposal must carry the originating PlanVersion's
    ``decision_run_id`` so SDD §6.11 lineage holds for speculation rows.
    """
    from argosy.orchestrator import speculation_router as router

    sess = session_with_current

    # Stamp the current PlanVersion with a real DecisionRun id (FK target).
    run = DecisionRun(
        user_id="ariel", ticker="(plan)", tier="T3",
        decision_kind="plan_revision", status="completed",
    )
    sess.add(run)
    sess.commit()
    sess.refresh(run)

    pv = sess.query(PlanVersion).filter_by(user_id="ariel", role="current").one()
    pv.decision_run_id = run.id
    sess.commit()

    captured: list[dict] = []
    monkeypatch.setattr(
        router, "_create_proposal",
        lambda **kw: (
            captured.append(kw)
            or _fake_proposal(proposal_id=42, account_class=kw["account_class"])
        ),
    )

    router.route_accepted_candidate(
        sess, user_id="ariel", ticker="HOOD", execution_mode="paper",
    )
    assert captured[0]["decision_run_id"] == run.id


def test_route_speculative_creates_real_proposal_with_lineage(session_with_current):
    """M2: integration test that exercises the *real*
    ``create_speculative_proposal`` helper end-to-end (no monkeypatch on
    ``_create_proposal``).  Verifies the persisted row has the expected
    column shape, the audit lineage column, and an ``expected_impact_json``
    that carries the exit/mode metadata (I2).
    """
    import json as _json

    from argosy.orchestrator import speculation_router as router
    from argosy.state.models import Proposal as ProposalRow

    sess = session_with_current

    # Stamp the current plan with a real DecisionRun id so the FK is valid.
    run = DecisionRun(
        user_id="ariel", ticker="(plan)", tier="T3",
        decision_kind="plan_revision", status="completed",
    )
    sess.add(run)
    sess.commit()
    sess.refresh(run)

    pv = sess.query(PlanVersion).filter_by(user_id="ariel", role="current").one()
    pv.decision_run_id = run.id
    sess.commit()

    out = router.route_accepted_candidate(
        sess, user_id="ariel", ticker="HOOD", execution_mode="paper",
    )

    persisted = sess.get(ProposalRow, out.proposal_id)
    assert persisted is not None
    assert persisted.ticker == "HOOD"
    assert persisted.account_class == "limited"
    assert persisted.tier == "T0"
    assert persisted.size_units == "currency"
    # I1: decision_run_id carried through.
    assert persisted.decision_run_id == run.id
    # I2: exit_trigger / execution_mode live in expected_impact_json now,
    # not as ``[exit]`` / ``[mode]`` prefix-stash inside rationale_summary.
    impact = _json.loads(persisted.expected_impact_json)
    assert impact["exit_trigger"] == "stop -20%, take +50%"
    assert impact["execution_mode"] == "paper"
    assert "[exit]" not in (persisted.rationale_summary or "")
    assert "[mode]" not in (persisted.rationale_summary or "")
