import json

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, User, PlanVersion, PlanNode, PlanEdge
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.state.graph_store import save_graph


def _session(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'gs.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.flush()
    pv = PlanVersion(user_id="ariel", role="draft")
    s.add(pv)
    s.flush()
    return s, pv.id


def _built_graph() -> DerivationGraph:
    g = DerivationGraph()
    g.add_node(Node(key="liquid_nw", kind=NodeKind.INPUT, value=11_687_926))
    g.add_node(Node(key="annual_spend", kind=NodeKind.INPUT, value=600_000))
    g.add_node(Node(
        key="fi_margin", kind=NodeKind.DERIVED, inputs=("liquid_nw", "annual_spend"),
        recipe=lambda i: i["liquid_nw"] / i["annual_spend"], compute_version="fi_v1",
    ))
    g.recompute()
    return g


def test_save_graph_writes_nodes_and_edges(tmp_path):
    s, plan_id = _session(tmp_path)
    save_graph(s, plan_id, _built_graph())
    s.commit()

    rows = s.execute(sa.select(PlanNode).where(PlanNode.plan_id == plan_id)).scalars().all()
    by_key = {r.node_key: r for r in rows}
    assert set(by_key) == {"liquid_nw", "annual_spend", "fi_margin"}
    assert by_key["liquid_nw"].kind == "input"
    assert by_key["liquid_nw"].status_validity == "valid"
    assert json.loads(by_key["liquid_nw"].value_json) == 11_687_926
    fi = by_key["fi_margin"]
    assert fi.kind == "derived"
    assert fi.compute_version == "fi_v1"
    assert fi.input_hash is not None and fi.status_validity == "valid"
    assert json.loads(fi.provenance_json)["recipe_key"] == "fi_margin"

    edges = s.execute(sa.select(PlanEdge).where(PlanEdge.plan_id == plan_id)).scalars().all()
    pairs = {(e.from_node_key, e.to_node_key) for e in edges}
    assert pairs == {("liquid_nw", "fi_margin"), ("annual_spend", "fi_margin")}


def test_save_graph_is_idempotent_replaces_prior(tmp_path):
    s, plan_id = _session(tmp_path)
    save_graph(s, plan_id, _built_graph())
    s.commit()
    save_graph(s, plan_id, _built_graph())  # second save must not duplicate
    s.commit()
    n = s.execute(
        sa.select(sa.func.count()).select_from(PlanNode).where(PlanNode.plan_id == plan_id)
    ).scalar_one()
    assert n == 3
