import pytest

from argosy.quality.change_adjudication import (
    ChangeRequest, ChangeKind, Author, AuthorKind,
)
from argosy.quality.change_request_store import (
    open_change_request, record_ladder_result, load_thread,
    supersede_change_request, ReopenError, assert_reopenable,
)
from argosy.orchestrator.flows.negotiation_ladder import (
    LadderResult, LadderTurn, Speaker, Stance, TerminalState,
)
from argosy.state.models import PlanVersion


SEED_USER = "test"  # db_session_with_seeded_user seeds User(id="test")


def _seed_plan(s):
    pv = PlanVersion(user_id=SEED_USER, version_label="t", role="current")
    s.add(pv)
    s.commit()
    s.refresh(pv)
    return pv.id


def _cr():
    return ChangeRequest(
        target_node_key="swr_pct",
        author=Author(kind=AuthorKind.AGENT, role="plan_critique"),
        kind=ChangeKind.SET_RECIPE,
        payload={"value": 0.04},
        rationale="over-conservative",
    )


def test_open_then_record_persists_terminal_state_and_turns(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    plan_id = _seed_plan(s)
    row_id = open_change_request(s, plan_id=plan_id, cr=_cr())
    result = LadderResult(
        terminal_state=TerminalState.B_CONCEDED,
        turns=[
            LadderTurn(0, Speaker.A, Stance.PROPOSE, "change swr_pct", ["swr_pct"]),
            LadderTurn(1, Speaker.B, Stance.CONCEDE, "agreed", ["swr_pct"]),
        ],
    )
    record_ladder_result(s, change_request_id=row_id, result=result)

    thread = load_thread(s, change_request_id=row_id)
    assert thread["status"] == "B_conceded"
    assert [t["speaker"] for t in thread["turns"]] == ["A", "B"]
    assert thread["turns"][0]["cited_nodes"] == ["swr_pct"]


def test_recorded_author_encodes_role(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    plan_id = _seed_plan(s)
    row_id = open_change_request(s, plan_id=plan_id, cr=_cr())
    thread = load_thread(s, change_request_id=row_id)
    assert thread["author"] == "agent:plan_critique"


def test_superseded_request_cannot_reopen(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    plan_id = _seed_plan(s)
    row_id = open_change_request(s, plan_id=plan_id, cr=_cr())
    supersede_change_request(s, change_request_id=row_id,
                             reason="resolved by a later input refresh")
    thread = load_thread(s, change_request_id=row_id)
    assert thread["status"] == "superseded"
    with pytest.raises(ReopenError):
        assert_reopenable(s, change_request_id=row_id)


def test_concluded_request_cannot_reopen(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    plan_id = _seed_plan(s)
    row_id = open_change_request(s, plan_id=plan_id, cr=_cr())
    record_ladder_result(s, change_request_id=row_id, result=LadderResult(
        terminal_state=TerminalState.ARBITER_RULED,
        turns=[LadderTurn(0, Speaker.A, Stance.PROPOSE, "x", ["swr_pct"])],
    ))
    with pytest.raises(ReopenError):
        assert_reopenable(s, change_request_id=row_id)


def test_in_dialogue_request_is_reopenable(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    plan_id = _seed_plan(s)
    row_id = open_change_request(s, plan_id=plan_id, cr=_cr())
    # Default status is "proposed" — not terminal, so reopen is allowed.
    assert_reopenable(s, change_request_id=row_id)  # no raise
