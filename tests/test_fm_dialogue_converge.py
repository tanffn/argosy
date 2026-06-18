import json

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import argosy.orchestrator.flows.fm_objection_dialogue as fod
from argosy.orchestrator.flows.fm_objection_dialogue import (
    StartResult, _terminal_state, converge_fm_objections,
)
from argosy.services.plan_export import render_fm_dialogue_appendix
from argosy.state.models import AgentReport, DecisionRun


# ---- the safety core: which (resolution, stance) clears the FM authority ----

def test_terminal_state_clears_only_accepted_rebuttal():
    assert _terminal_state("FM_ACCEPTS_ANALYST", "REBUT") == "CLEARED_NO_CHANGE_REQUIRED"
    assert _terminal_state("FM_ACCEPTS_ANALYST", "CLARIFY") == "CLEARED_NO_CHANGE_REQUIRED"


def test_terminal_state_accepted_concede_is_a_defect_not_a_clear():
    # codex guardrail: FM accepting an analyst who CONCEDED = confirmed defect → blocking.
    assert _terminal_state("FM_ACCEPTS_ANALYST", "CONCEDE") == "CHANGE_REQUIRED"


def test_terminal_state_revise_maintain_escalate_block():
    assert _terminal_state("FM_REVISES_OBJECTION", "REBUT") == "REVISED_BLOCKING"
    assert _terminal_state("FM_MAINTAINS_OBJECTION", "REBUT") == "MAINTAINED_BLOCKING"
    assert _terminal_state("ESCALATE_TO_USER", "REBUT") == "ESCALATE_TO_USER"


# ---- the report visualization ----

def test_appendix_renders_dialogue_rows():
    rows = [
        {"notes": {"objection_index": 0, "objection_topic": "NVDA pace too slow",
                   "analyst_role": "concentration", "analyst_stance": "REBUT",
                   "resolution": "FM_ACCEPTS_ANALYST"}},
        {"notes": {"objection_index": 1, "objection_topic": "FI margin thin",
                   "analyst_role": "fi_methodology", "analyst_stance": "CONCEDE",
                   "resolution": "FM_ACCEPTS_ANALYST"}},
    ]
    md = render_fm_dialogue_appendix(rows)
    assert "how the FM talked to the fleet" in md
    assert "concentration" in md and "REBUT" in md
    assert "cleared (no change needed)" in md           # row 0
    assert "defect confirmed — change required" in md    # row 1 (CONCEDE)


def test_appendix_empty_is_noop():
    assert render_fm_dialogue_appendix([]) == ""


# ---- converge integration (stubbed dialogues, no live LLM) ----

def _db():
    eng = sa.create_engine("sqlite://", connect_args={"check_same_thread": False})
    from argosy.state.models import User
    User.__table__.create(eng); AgentReport.__table__.create(eng); DecisionRun.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    s.add(User(id="ariel", plan="free")); s.commit()
    return s


def _seed_fm(s, reasons):
    s.add(AgentReport(user_id="ariel", agent_role="fund_manager",
                      decision_id="plan-synth-99", prompt_hash="h",
                      response_text=json.dumps({"approved": False, "reasons": reasons})))
    s.commit()


def _stub_start(monkeypatch, outcomes):
    """outcomes: list of (resolution, stance) per dialogue, in dispatch order."""
    calls = {"i": 0}

    def _fake(session, **kw):
        i = calls["i"]; calls["i"] += 1
        resolution, stance = outcomes[i]
        run = DecisionRun(user_id="ariel", ticker="(plan)", decision_kind="fm_objection_dialogue",
                          status="completed",
                          notes_json=json.dumps({"objection_index": kw["objection_index"],
                                                 "analyst_role": kw["analyst_role"],
                                                 "resolution": resolution,
                                                 "analyst_stance": stance}))
        session.add(run); session.commit(); session.refresh(run)
        return StartResult(decision_run_id=run.id, inflight=False)

    monkeypatch.setattr(fod, "start_fm_objection_dialogue", _fake)
    # objections carry an analyst owner via the citation form the parser recognizes.
    monkeypatch.setattr("argosy.api.routes.plan._parse_fm_response",
                        lambda txt: json.loads(txt))
    monkeypatch.setattr("argosy.api.routes.plan._split_reason",
                        lambda raw: (raw, "agent_report:concentration"))
    monkeypatch.setattr("argosy.api.routes.plan._classify_severity", lambda t, d: "BLOCKER")


def test_converge_all_cleared_sets_all_agreed(monkeypatch):
    s = _db(); _seed_fm(s, ["pace", "fx"])
    _stub_start(monkeypatch, [("FM_ACCEPTS_ANALYST", "REBUT"), ("FM_ACCEPTS_ANALYST", "CLARIFY")])
    res = converge_fm_objections(s, user_id="ariel", plan_version_id=47, decision_run_id=99)
    assert res.dispatched == 2 and res.all_agreed is True and res.unresolved == []


def test_converge_one_concede_blocks(monkeypatch):
    s = _db(); _seed_fm(s, ["pace", "fx"])
    _stub_start(monkeypatch, [("FM_ACCEPTS_ANALYST", "REBUT"), ("FM_ACCEPTS_ANALYST", "CONCEDE")])
    res = converge_fm_objections(s, user_id="ariel", plan_version_id=47, decision_run_id=99)
    assert res.all_agreed is False
    assert any("CHANGE_REQUIRED" in u for u in res.unresolved)
