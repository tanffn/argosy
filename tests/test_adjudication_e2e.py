from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.change_adjudication import (
    ChangeRequest, ChangeKind, Author, AuthorKind,
    OwnershipMap, adjudicate, Disposition, HardNodeError, assert_resolvable,
)
from argosy.orchestrator.flows.negotiation_ladder import (
    run_ladder, PeerVerdict, ArbiterClass, TerminalState,
)
from argosy.quality.change_request_store import (
    open_change_request, record_ladder_result, load_thread,
)
from argosy.state.models import PlanVersion


class _Parts:
    def __init__(self, peer, ac=None, ruling=""):
        self._peer, self._ac, self._ruling = list(peer), ac, ruling
    def peer_round(self, *, change, prior_turns, round):
        return self._peer[round - 1]
    def arbiter(self, *, change, prior_turns):
        return self._ac, self._ruling


SEED_USER = "test"  # db_session_with_seeded_user seeds User(id="test")


def _seed_plan(s):
    pv = PlanVersion(user_id=SEED_USER, version_label="t", role="current")
    s.add(pv)
    s.commit()
    s.refresh(pv)
    return pv.id


def test_recipe_change_full_path_to_persisted_arbiter_ruling(db_session_with_seeded_user):
    s = db_session_with_seeded_user
    g = DerivationGraph()
    g.add_node(Node(key="swr_pct", kind=NodeKind.INPUT, value=0.035))
    om = OwnershipMap(g, recipe_node_keys={"swr_pct"})
    cr = ChangeRequest(
        target_node_key="swr_pct",
        author=Author(kind=AuthorKind.AGENT, role="plan_critique"),
        kind=ChangeKind.SET_RECIPE, payload={"value": 0.04},
        rationale="over-conservative",
    )
    assert adjudicate(cr, om).disposition is Disposition.NEEDS_LADDER

    parts = _Parts(
        [(PeerVerdict.UNRESOLVED, "no")] * 3,
        ac=ArbiterClass.EVIDENCE_RESOLVABLE, ruling="re-derive; 0.04 supported",
    )
    result = run_ladder(cr, parts)
    assert result.terminal_state is TerminalState.ARBITER_RULED

    plan_id = _seed_plan(s)
    row_id = open_change_request(s, plan_id=plan_id, cr=cr)
    record_ladder_result(s, change_request_id=row_id, result=result)
    thread = load_thread(s, change_request_id=row_id)
    assert thread["status"] == "arbiter_ruled"
    # Full replayable transcript: A propose -> 3 B rebuts -> arbiter classify+rule.
    speakers = [t["speaker"] for t in thread["turns"]]
    assert speakers[0] == "A"
    assert speakers.count("B") == 3
    assert "arbiter" in speakers


def test_public_exports():
    import argosy.quality.change_adjudication as ca
    import argosy.orchestrator.flows.negotiation_ladder as nl
    import argosy.quality.change_request_store as crs
    import argosy.quality.publish_gate as pg
    for name in ("ChangeRequest", "adjudicate", "OwnershipMap", "Disposition",
                 "HardNodeError", "assert_resolvable"):
        assert name in ca.__all__
    for name in ("run_ladder", "LadderResult", "TerminalState", "LadderParticipants"):
        assert name in nl.__all__
    for name in ("open_change_request", "record_ladder_result", "load_thread",
                 "supersede_change_request", "assert_reopenable", "ReopenError"):
        assert name in crs.__all__
    assert "can_publish_plan" in pg.__all__
