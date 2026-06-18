import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, User, PlanVersion
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.state.graph_store import save_graph, apply_change, replay_cycle


def _session(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'rep.db'}", connect_args={"check_same_thread": False})
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
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",),
                    recipe=lambda i: i["x"] + 1, compute_version="v1"))
    g.add_node(Node(key="a", kind=NodeKind.INPUT, value=5))
    g.add_node(Node(key="b", kind=NodeKind.DERIVED, inputs=("a",),
                    recipe=lambda i: i["a"] * 10, compute_version="v1"))
    g.recompute()
    return g


def test_replay_reconstructs_ordered_ripple(tmp_path):
    s, plan_id = _session(tmp_path)
    g = _graph()
    save_graph(s, plan_id, g)
    s.commit()

    apply_change(s, plan_id, g, cycle_id="cyc", trigger_node_key="x", new_value=100,
                 verification_verdicts={"gate": "pass"})
    apply_change(s, plan_id, g, cycle_id="cyc", trigger_node_key="a", new_value=7,
                 verification_verdicts={"gate": "pass"})
    s.commit()

    steps = replay_cycle(s, plan_id, "cyc")
    assert [st.trigger_node_key for st in steps] == ["x", "a"]  # chronological

    first = steps[0]
    assert first.invalidated == ["y"]
    assert first.recomputed == {"y": {"old": 2, "new": 101}}
    assert first.rerendered == []
    assert first.verdicts == {"gate": "pass"}

    second = steps[1]
    assert second.invalidated == ["b"]
    assert second.recomputed == {"b": {"old": 50, "new": 70}}


def test_replay_unknown_cycle_is_empty(tmp_path):
    s, plan_id = _session(tmp_path)
    assert replay_cycle(s, plan_id, "nope") == []
