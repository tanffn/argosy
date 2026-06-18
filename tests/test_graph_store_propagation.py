import json

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, User, PlanVersion, PropagationEvent
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.state.graph_store import save_graph, apply_change


def _session(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'prop.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.flush()
    pv = PlanVersion(user_id="ariel", role="draft")
    s.add(pv)
    s.flush()
    return s, pv.id


def _graph() -> DerivationGraph:
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=10))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",),
                    recipe=lambda i: i["x"] + 1, compute_version="y_v1"))
    g.add_node(Node(key="surf_y", kind=NodeKind.SURFACE, inputs=("y",),
                    recipe=lambda i: f"y is {i['y']}", compute_version="s_v1"))
    g.add_node(Node(key="indep", kind=NodeKind.INPUT, value=99))
    g.add_node(Node(key="w", kind=NodeKind.DERIVED, inputs=("indep",),
                    recipe=lambda i: i["indep"] * 2, compute_version="w_v1"))
    g.recompute()
    return g


def test_apply_change_emits_event_matching_closure(tmp_path):
    s, plan_id = _session(tmp_path)
    g = _graph()
    save_graph(s, plan_id, g)
    s.commit()

    event = apply_change(
        s, plan_id, g, cycle_id="cycle-1",
        trigger_node_key="x", new_value=100,
        verification_verdicts={"coherence_gate": "pass"},
    )
    s.commit()

    # The returned event mirrors the persisted row.
    row = s.execute(
        sa.select(PropagationEvent).where(PropagationEvent.plan_id == plan_id)
    ).scalar_one()
    assert row.trigger_node_key == "x"
    assert row.cycle_id == "cycle-1"

    invalidated = set(json.loads(row.invalidated_node_keys_json))
    recomputed = json.loads(row.recomputed_json)
    rerendered = set(json.loads(row.rerendered_surfaces_json))
    verdicts = json.loads(row.verification_verdicts_json)

    # EXACTLY x's transitive dependents were invalidated — not w/indep.
    assert invalidated == {"y", "surf_y"}
    assert "w" not in invalidated and "indep" not in invalidated
    # recomputed carries old->new for every recomputed node.
    assert set(recomputed) == {"y", "surf_y"}
    assert recomputed["y"] == {"old": 11, "new": 101}
    assert recomputed["surf_y"] == {"old": "y is 11", "new": "y is 101"}
    # rerendered surfaces = the SURFACE nodes in the closure.
    assert rerendered == {"surf_y"}
    assert verdicts == {"coherence_gate": "pass"}

    # The engine actually applied the change.
    assert g.get("y").value == 101
    assert g.is_closed() is True


def test_apply_change_persists_updated_graph(tmp_path):
    s, plan_id = _session(tmp_path)
    g = _graph()
    save_graph(s, plan_id, g)
    s.commit()
    apply_change(s, plan_id, g, cycle_id="c", trigger_node_key="x", new_value=100)
    s.commit()

    from argosy.state.models import PlanNode
    y_row = s.execute(
        sa.select(PlanNode).where(PlanNode.plan_id == plan_id, PlanNode.node_key == "y")
    ).scalar_one()
    assert json.loads(y_row.value_json) == 101
    assert y_row.status_validity == "valid"
