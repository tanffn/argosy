import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, User, PlanVersion
from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.state.graph_store import save_graph, load_graph


def _session(tmp_path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'rt.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    s = SF()
    s.add(User(id="ariel", plan="free"))
    s.flush()
    pv = PlanVersion(user_id="ariel", role="draft")
    s.add(pv)
    s.flush()
    return s, pv.id


def _fi_recipe(i):
    return i["liquid_nw"] / i["annual_spend"]


def _built_graph() -> DerivationGraph:
    g = DerivationGraph()
    g.add_node(Node(key="liquid_nw", kind=NodeKind.INPUT, value=12_000_000))
    g.add_node(Node(key="annual_spend", kind=NodeKind.INPUT, value=600_000))
    g.add_node(Node(key="fi_margin", kind=NodeKind.DERIVED,
                    inputs=("liquid_nw", "annual_spend"),
                    recipe=_fi_recipe, compute_version="fi_v1"))
    g.recompute()
    return g


def test_roundtrip_preserves_values_and_validity(tmp_path):
    s, plan_id = _session(tmp_path)
    save_graph(s, plan_id, _built_graph())
    s.commit()

    registry = {"fi_margin": _fi_recipe}
    g2 = load_graph(s, plan_id, recipe_registry=registry)

    assert g2.get("liquid_nw").value == 12_000_000
    assert g2.get("fi_margin").value == 20.0
    assert g2.get("fi_margin").inputs == ("liquid_nw", "annual_spend") or \
        set(g2.get("fi_margin").inputs) == {"liquid_nw", "annual_spend"}
    # The reloaded graph is already closed — no recompute needed.
    assert g2.is_closed() is True
    assert g2.is_valid("fi_margin") is True


def test_roundtrip_then_change_input_recomputes_correctly(tmp_path):
    s, plan_id = _session(tmp_path)
    save_graph(s, plan_id, _built_graph())
    s.commit()

    g2 = load_graph(s, plan_id, recipe_registry={"fi_margin": _fi_recipe})
    invalidated = g2.set_input("annual_spend", 1_200_000)
    assert invalidated == {"fi_margin"}
    g2.recompute()
    assert g2.get("fi_margin").value == 10.0  # 12_000_000 / 1_200_000


def test_load_missing_recipe_flags_node(tmp_path):
    s, plan_id = _session(tmp_path)
    save_graph(s, plan_id, _built_graph())
    s.commit()
    # Empty registry — the derived node's recipe cannot be re-attached.
    g2 = load_graph(s, plan_id, recipe_registry={})
    n = g2.get("fi_margin")
    assert n.recipe is None
    # It keeps its persisted value but is NOT silently an INPUT.
    assert n.kind is NodeKind.DERIVED
    assert n.value == 20.0
