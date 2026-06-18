import pytest
from argosy.quality.derivation_graph import (
    DerivationGraph, Node, NodeKind, UnknownNodeError, CycleError,
)


# --- Task 1: skeleton -------------------------------------------------------

def test_add_and_get_node():
    g = DerivationGraph()
    g.add_node(Node(key="liquid_nw", kind=NodeKind.INPUT, value=11_687_926))
    n = g.get("liquid_nw")
    assert n.kind is NodeKind.INPUT
    assert n.value == 11_687_926


def test_get_unknown_node_raises():
    g = DerivationGraph()
    with pytest.raises(UnknownNodeError):
        g.get("nope")


# --- Task 2: version-stamped hashing ---------------------------------------

def test_hash_changes_with_input_value():
    g = DerivationGraph()
    g.add_node(Node(key="a", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="b", kind=NodeKind.INPUT, value=2))
    g.add_node(Node(key="sum", kind=NodeKind.DERIVED, inputs=("a", "b")))
    h1 = g.hash_of("sum")
    g.get("a").value = 99
    assert g.hash_of("sum") != h1


def test_hash_changes_with_collection_membership():
    g = DerivationGraph()
    g.add_node(Node(key="lots", kind=NodeKind.INPUT, value=[1, 2, 3]))
    g.add_node(Node(key="total", kind=NodeKind.DERIVED, inputs=("lots",)))
    h1 = g.hash_of("total")
    g.get("lots").value = [1, 2, 3, 4]  # a new lot joined the collection
    assert g.hash_of("total") != h1


def test_hash_changes_with_compute_version():
    g = DerivationGraph()
    g.add_node(Node(key="a", kind=NodeKind.INPUT, value=1))
    n = Node(key="d", kind=NodeKind.DERIVED, inputs=("a",), compute_version="v1")
    g.add_node(n)
    h1 = g.hash_of("d")
    n.compute_version = "v2"  # the recipe changed; inputs did not
    assert g.hash_of("d") != h1


def test_hash_is_stable_and_order_independent():
    g = DerivationGraph()
    g.add_node(Node(key="a", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="b", kind=NodeKind.INPUT, value=2))
    g.add_node(Node(key="d1", kind=NodeKind.DERIVED, inputs=("a", "b")))
    g.add_node(Node(key="d2", kind=NodeKind.DERIVED, inputs=("b", "a")))
    assert g.hash_of("d1") == g.hash_of("d2")  # input order must not matter
    assert g.hash_of("d1") == g.hash_of("d1")  # stable across calls


# --- Task 3: transitive dependents + cycle detection -----------------------

def test_transitive_dependents():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",)))
    g.add_node(Node(key="z", kind=NodeKind.DERIVED, inputs=("y",)))
    g.add_node(Node(key="other", kind=NodeKind.INPUT, value=9))
    assert g.dependents("x") == {"y", "z"}
    assert g.dependents("y") == {"z"}
    assert g.dependents("other") == set()


def test_cycle_is_detected():
    g = DerivationGraph()
    g.add_node(Node(key="a", kind=NodeKind.DERIVED, inputs=("b",)))
    g.add_node(Node(key="b", kind=NodeKind.DERIVED, inputs=("a",)))
    with pytest.raises(CycleError):
        g.check_acyclic()


# --- Task 4: validity + exact invalidation ---------------------------------

def test_input_node_is_always_valid():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    assert g.is_valid("x") is True


def test_uncomputed_derived_is_invalid():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"] + 1))
    assert g.is_valid("y") is False  # input_hash is None until computed


def test_set_input_rejects_non_input():
    g = DerivationGraph()
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=()))
    with pytest.raises(ValueError):
        g.set_input("y", 5)


def test_set_input_invalidates_exactly_the_dependents():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"] + 1))
    g.add_node(Node(key="z", kind=NodeKind.DERIVED, inputs=("y",), recipe=lambda i: i["y"] * 2))
    g.add_node(Node(key="indep", kind=NodeKind.INPUT, value=5))
    g.add_node(Node(key="w", kind=NodeKind.DERIVED, inputs=("indep",), recipe=lambda i: i["indep"]))
    g.recompute()
    assert all(g.is_valid(k) for k in ("y", "z", "w"))
    invalidated = g.set_input("x", 100)
    assert invalidated == {"y", "z"}      # exactly x's dependents
    assert g.is_valid("w") is True        # untouched — not downstream of x
    assert g.is_valid("y") is False
    assert g.is_valid("z") is False


# --- Task 5: deterministic topological recompute ---------------------------

def test_recompute_computes_in_dependency_order():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=10))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"] + 1))
    g.add_node(Node(key="z", kind=NodeKind.DERIVED, inputs=("y",), recipe=lambda i: i["y"] * 2))
    recomputed = g.recompute()
    assert g.get("y").value == 11
    assert g.get("z").value == 22
    assert recomputed.index("y") < recomputed.index("z")  # y before z
    assert all(g.is_valid(k) for k in ("x", "y", "z"))


def test_recompute_only_touches_stale_nodes():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"] + 1))
    g.add_node(Node(key="indep", kind=NodeKind.INPUT, value=5))
    g.add_node(Node(key="w", kind=NodeKind.DERIVED, inputs=("indep",), recipe=lambda i: i["indep"]))
    g.recompute()                      # everything valid
    g.set_input("x", 100)              # only y stale now
    recomputed = g.recompute()
    assert recomputed == ["y"]         # w NOT recomputed (independent)
    assert g.get("y").value == 101


def test_recompute_is_deterministic():
    def build():
        g = DerivationGraph()
        g.add_node(Node(key="a", kind=NodeKind.INPUT, value=3))
        g.add_node(Node(key="b", kind=NodeKind.INPUT, value=4))
        g.add_node(Node(key="s", kind=NodeKind.DERIVED, inputs=("a", "b"),
                        recipe=lambda i: i["a"] + i["b"]))
        g.recompute()
        return g.get("s").value
    assert build() == build() == 7


# --- Task 6: graph expansion -----------------------------------------------

def test_adding_a_node_at_runtime_expands_and_recomputes():
    # A new holding arrives: add its value node + grow the collection it belongs
    # to. Its surface row is new+invalid; the collection's dependents go stale.
    g = DerivationGraph()
    g.add_node(Node(key="holdings", kind=NodeKind.INPUT, value=["NVDA", "AMD"]))
    g.add_node(Node(key="count", kind=NodeKind.DERIVED, inputs=("holdings",),
                    recipe=lambda i: len(i["holdings"])))
    g.recompute()
    assert g.get("count").value == 2

    # Expansion: a new holding joins.
    g.add_node(Node(key="row:GOOG", kind=NodeKind.SURFACE, inputs=("holdings",),
                    recipe=lambda i: "GOOG in book" if "GOOG" in i["holdings"] else "absent"))
    invalidated = g.set_input("holdings", ["NVDA", "AMD", "GOOG"])
    assert "count" in invalidated
    recomputed = g.recompute()
    assert g.get("count").value == 3          # membership change propagated
    assert g.get("row:GOOG").value == "GOOG in book"
    assert "row:GOOG" in recomputed and "count" in recomputed


def test_is_closed_reflects_pending_new_node():
    g = DerivationGraph()
    g.add_node(Node(key="x", kind=NodeKind.INPUT, value=1))
    g.add_node(Node(key="y", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"]))
    g.recompute()
    assert g.is_closed() is True
    g.add_node(Node(key="z", kind=NodeKind.DERIVED, inputs=("x",), recipe=lambda i: i["x"] * 3))
    assert g.is_closed() is False             # new node not yet computed
    g.recompute()
    assert g.is_closed() is True


# --- Task 7: public exports ------------------------------------------------

def test_public_exports():
    import argosy.quality.derivation_graph as dg
    for name in ("DerivationGraph", "Node", "NodeKind",
                 "UnknownNodeError", "CycleError"):
        assert name in dg.__all__
