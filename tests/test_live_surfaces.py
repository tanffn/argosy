"""Canonical live surfaces — two surfaces over the SAME derived node can never
contradict each other (the single-node-render guarantee)."""
from __future__ import annotations

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.live_surfaces import (
    CANONICAL_SUBJECT_NODE,
    EARLIEST_SAFE_AGE_NODE,
    FI_CROSSING_YEAR_NODE,
    FI_MARGIN_NODE,
    NET_WORTH_INVESTABLE_NODE,
    NET_WORTH_LIQUID_NODE,
    NET_WORTH_TOTAL_NODE,
    RETENTION_AT_VEST_NODE,
    RETENTION_CAPITAL_TRACK_NODE,
    register_canonical_surfaces,
)
from argosy.quality.coherence.surface_registry import SUBJECT_REGISTRY


def _input_node(key: str, value):
    return Node(key=key, kind=NodeKind.INPUT, value=value)


def _build_graph_with_canonical_inputs() -> DerivationGraph:
    """A hermetic graph: the canonical derived nodes are seeded as INPUT nodes
    (so the test can set their value directly) + all canonical surfaces."""
    g = DerivationGraph()
    for node_key in set(CANONICAL_SUBJECT_NODE.values()):
        g.add_node(_input_node(node_key, 0.0))
    register_canonical_surfaces(g)
    g.recompute()
    return g


def test_fi_verdict_and_dashboard_tile_share_basis_and_sign() -> None:
    """The FI verdict and the dashboard FI tile both render from the ONE
    fi_margin node. Changing the margin updates BOTH identically — no basis-flip
    (reached on one, short on the other) is possible."""
    g = _build_graph_with_canonical_inputs()

    # Positive margin -> both must say REACHED.
    g.set_input(FI_MARGIN_NODE, 500_000.0)
    g.recompute()
    verdict = g.get("surface:fi_verdict").value
    tile = g.get("surface:dashboard.fi_tile").value
    assert "REACHED" in verdict
    assert "REACHED" in tile
    assert verdict == tile  # same node, same recipe -> byte-identical

    # Flip the sign -> BOTH flip together to NOT reached. No surface lags.
    g.set_input(FI_MARGIN_NODE, -750_000.0)
    g.recompute()
    verdict2 = g.get("surface:fi_verdict").value
    tile2 = g.get("surface:dashboard.fi_tile").value
    assert "NOT reached" in verdict2
    assert "NOT reached" in tile2
    assert verdict2 == tile2
    # The appendix row reads the SAME magnitude.
    appendix = g.get("surface:appendix.fi_table").value
    assert "750,000" in appendix


def test_retirement_age_headline_and_dashboard_show_identical_age() -> None:
    """The headline age and the dashboard age tile both render the SAME
    earliest_safe_age node -> identical age (kills the 46-vs-dashboard
    divergence)."""
    g = _build_graph_with_canonical_inputs()

    g.set_input(EARLIEST_SAFE_AGE_NODE, 46)
    g.recompute()
    headline = g.get("surface:retirement_age_headline").value
    tile = g.get("surface:dashboard.age_tile").value
    assert "46" in headline
    assert "46" in tile

    # Re-derive to 47 -> BOTH move together; no surface can pin a stale 46.
    g.set_input(EARLIEST_SAFE_AGE_NODE, 47)
    g.recompute()
    assert "47" in g.get("surface:retirement_age_headline").value
    assert "47" in g.get("surface:dashboard.age_tile").value
    assert "46" not in g.get("surface:dashboard.age_tile").value


def test_each_canonicalized_subject_maps_to_exactly_one_node() -> None:
    """Every subject this module claims to canonicalize maps to exactly one node
    key, and (where it overlaps a SUBJECT_REGISTRY subject_type) that registry
    subject is a real one — the unification is one source per subject."""
    # Each registration reports exactly one node key per subject.
    g = _build_graph_with_canonical_inputs()
    regs = register_canonical_surfaces(g)
    by_subject: dict[str, set[str]] = {}
    for r in regs:
        by_subject.setdefault(r.subject_type, set()).add(r.node_key)
    for subject, node_keys in by_subject.items():
        assert len(node_keys) == 1, f"{subject} maps to >1 node: {node_keys}"

    # The mapping itself has exactly one node per subject.
    for subject, node_key in CANONICAL_SUBJECT_NODE.items():
        assert isinstance(node_key, str) and node_key

    # _SUBJECT_BUILDERS and CANONICAL_SUBJECT_NODE must stay in lock-step: every
    # subject with a builder has a node mapping and vice-versa (else a new subject
    # silently lacks surfaces or a node).
    assert set(by_subject) == set(CANONICAL_SUBJECT_NODE)

    # The two distinct net-worth bases map to DIFFERENT nodes (distinct labels).
    assert (
        CANONICAL_SUBJECT_NODE["net_worth_liquid"]
        != CANONICAL_SUBJECT_NODE["net_worth_investable"]
    )

    # Subjects that overlap the coherence SUBJECT_REGISTRY must name a real
    # registry subject_type (unification across the two registries).
    overlapping = set(CANONICAL_SUBJECT_NODE) & set(SUBJECT_REGISTRY)
    assert "fi_capital_sufficiency" in overlapping
    assert "retirement_age_headline" in overlapping
    for subject in overlapping:
        # exactly one canonical node for the shared subject
        assert CANONICAL_SUBJECT_NODE[subject]


def test_net_worth_total_basis_renders_distinct_label() -> None:
    """The total (incl. residence) net-worth basis renders from its OWN node,
    distinctly labelled 'total' so it can never be confused with the liquid or
    investable basis (the ₪14.05M-vs-₪11.87M contradiction)."""
    g = _build_graph_with_canonical_inputs()
    g.set_input(NET_WORTH_TOTAL_NODE, 14_050_000.0)
    g.recompute()
    tile = g.get("surface:dashboard.net_worth_total_tile").value
    appendix = g.get("surface:appendix.net_worth_total").value
    assert "14,050,000" in tile
    assert "total" in tile.lower()
    assert "14,050,000" in appendix
    assert NET_WORTH_TOTAL_NODE not in (NET_WORTH_LIQUID_NODE, NET_WORTH_INVESTABLE_NODE)


def test_fi_crossing_surface_renders_future_year_and_handles_pending() -> None:
    """The FI-crossing statement + tile render from the ONE reconciled
    crossing-year node. A resolved future year renders that year; the fail-closed
    0.0 seed (a pending crossing — FI not reached within the horizon) renders an
    explicit 'not reached' / 'beyond horizon' string, never 'year 0'."""
    g = _build_graph_with_canonical_inputs()
    g.set_input(FI_CROSSING_YEAR_NODE, 2027.0)
    g.recompute()
    stmt = g.get("surface:fi_crossing_statement").value
    tile = g.get("surface:dashboard.fi_crossing_tile").value
    assert "2027" in stmt and "crossing" in stmt.lower()
    assert "2027" in tile

    # Fail-closed seed (pending) -> explicit not-reached text on BOTH; no 'year 0'.
    g.set_input(FI_CROSSING_YEAR_NODE, 0.0)
    g.recompute()
    pending = g.get("surface:fi_crossing_statement").value
    pending_tile = g.get("surface:dashboard.fi_crossing_tile").value
    assert "year 0" not in pending.lower() and ": 0" not in pending
    assert "not reached" in pending.lower() and "horizon" in pending.lower()
    assert "beyond horizon" in pending_tile.lower()


def test_fi_crossing_rejects_non_integer_and_non_finite_years() -> None:
    """A stale/injected non-integer (2026.5) or non-finite (inf) year is treated
    as pending — never truncated to a present-crossing contradiction, never an
    int() crash on recompute (codex impl-review #2)."""
    import math
    from argosy.quality.live_surfaces import valid_crossing_year
    assert valid_crossing_year(2027.0) is True
    assert valid_crossing_year(2027) is True
    for bad in (2026.5, math.inf, math.nan, 0.0, 1999.0, True):
        assert valid_crossing_year(bad) is False
    g = _build_graph_with_canonical_inputs()
    g.set_input(FI_CROSSING_YEAR_NODE, 2026.5)
    g.recompute()  # must not raise
    assert "not reached" in g.get("surface:fi_crossing_statement").value.lower()
    assert "beyond horizon" in g.get("surface:dashboard.fi_crossing_tile").value.lower()


def test_retention_rates_render_as_two_distinct_labelled_surfaces() -> None:
    """The at-vest (ordinary) and capital-track (Section-102) retention rates
    render from two SEPARATE nodes, each distinctly labelled, so prose can never
    conflate 50% and 70% into one ambiguous 'retention'."""
    g = _build_graph_with_canonical_inputs()
    g.set_input(RETENTION_AT_VEST_NODE, 0.50)
    g.set_input(RETENTION_CAPITAL_TRACK_NODE, 0.70)
    g.recompute()
    at_vest = g.get("surface:retention_at_vest_statement").value
    cap = g.get("surface:retention_capital_track_statement").value
    assert "50%" in at_vest and "at-vest" in at_vest.lower()
    assert "70%" in cap and "capital" in cap.lower()
    assert RETENTION_AT_VEST_NODE != RETENTION_CAPITAL_TRACK_NODE
    g.set_input(RETENTION_AT_VEST_NODE, 0.40)
    g.recompute()
    assert "70%" in g.get("surface:retention_capital_track_statement").value


def test_retention_pending_seed_does_not_render_a_false_zero_rate() -> None:
    """The fail-closed 0.0 seed (pending / omitted resolver value) must NOT render
    as a live '0%' statutory rate — it renders an explicit pending string.
    Anything outside (0, 1] is treated as pending."""
    g = _build_graph_with_canonical_inputs()
    g.set_input(RETENTION_AT_VEST_NODE, 0.0)
    g.set_input(RETENTION_CAPITAL_TRACK_NODE, 1.5)  # >1 is also invalid
    g.recompute()
    at_vest = g.get("surface:retention_at_vest_statement").value
    cap = g.get("surface:retention_capital_track_statement").value
    assert "0%" not in at_vest and "pending" in at_vest.lower()
    assert "pending" in cap.lower()
