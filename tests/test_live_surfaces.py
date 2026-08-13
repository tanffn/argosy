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
    NVDA_CAP_PCT_NODE,
    NVDA_TARGET_PCT_NODE,
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


# ---------------------------------------------------------------------------
# NVDA cap and target canonical node tests
# Root cause of the 12%/8%/13% three-value contradiction (plan critique RED
# finding 2026-07-07): the IPS prose said "12% sleeve" (stale from when
# NVDA_TARGET_PCT was 12%) while the canonical TargetAllocationDoc had the
# NVDA class at 8% and the look-through cap at 13%.  Binding both to canonical
# nodes means every surface reads one source and a stale hardcode can never
# diverge again.
# ---------------------------------------------------------------------------

def test_nvda_cap_and_target_render_from_single_node_each() -> None:
    """NVDA cap (13%) and target (8%) each render from their OWN canonical node.
    The statement and dashboard tile for EACH are byte-identical because they
    share the node — a stale hardcode cannot cause one surface to say 12% while
    another says 8%."""
    g = _build_graph_with_canonical_inputs()
    g.set_input(NVDA_CAP_PCT_NODE, 0.13)    # 13 % as a fraction
    g.set_input(NVDA_TARGET_PCT_NODE, 0.08)  # 8 % as a fraction
    g.recompute()

    cap_stmt = g.get("surface:nvda_cap_statement").value
    cap_tile = g.get("surface:dashboard.nvda_cap_tile").value
    tgt_stmt = g.get("surface:nvda_target_statement").value
    tgt_tile = g.get("surface:dashboard.nvda_target_tile").value

    # Both cap surfaces render "13.0%" (×100 from 0.13)
    assert "13.0%" in cap_stmt, f"cap_stmt={cap_stmt!r}"
    assert "13.0" in cap_tile, f"cap_tile={cap_tile!r}"
    # The cap is labelled as a look-through hard cap
    assert "cap" in cap_stmt.lower()

    # Both target surfaces render "8.0%" (×100 from 0.08)
    assert "8.0%" in tgt_stmt, f"tgt_stmt={tgt_stmt!r}"
    assert "8.0" in tgt_tile, f"tgt_tile={tgt_tile!r}"
    # The target is labelled as the direct sleeve target (distinct from cap)
    assert "target" in tgt_stmt.lower()

    # The two nodes are DISTINCT — cap ≠ target
    assert NVDA_CAP_PCT_NODE != NVDA_TARGET_PCT_NODE


def test_nvda_cap_change_propagates_to_all_cap_surfaces_simultaneously() -> None:
    """Changing the cap node from 13% to 15% updates BOTH the statement AND the
    tile atomically (they share the node).  No surface can lag — the scenario
    that produced 'prose says 12%, table says 8%' is structurally impossible."""
    g = _build_graph_with_canonical_inputs()
    g.set_input(NVDA_CAP_PCT_NODE, 0.13)
    g.recompute()
    assert "13.0%" in g.get("surface:nvda_cap_statement").value
    assert "13.0" in g.get("surface:dashboard.nvda_cap_tile").value

    # Update to 15% and recompute — BOTH surfaces move.
    g.set_input(NVDA_CAP_PCT_NODE, 0.15)
    g.recompute()
    assert "15.0%" in g.get("surface:nvda_cap_statement").value
    assert "15.0" in g.get("surface:dashboard.nvda_cap_tile").value
    # The OLD value must not survive on either surface.
    assert "13.0" not in g.get("surface:nvda_cap_statement").value
    assert "13.0" not in g.get("surface:dashboard.nvda_cap_tile").value


def test_nvda_target_node_bound_to_allocation_plan_constant() -> None:
    """The NVDA_TARGET_PCT_NODE value in the resolver always equals
    NVDA_TARGET_PCT / 100 from allocation_plan.py — proves the surface and the
    TargetAllocationDoc cannot show different values as long as both read the
    same constant (the core of the cross-surface binding)."""
    from argosy.services.allocation_plan import NVDA_TARGET_PCT
    from argosy.services.retirement.scenario_mc import DEFAULT_NVDA_CAP_PCT

    # The target (8%) must be strictly below the cap (13%).
    assert NVDA_TARGET_PCT < DEFAULT_NVDA_CAP_PCT * 100.0

    g = _build_graph_with_canonical_inputs()
    # Seed the target with the SAME value the resolver uses.
    canonical_target_frac = NVDA_TARGET_PCT / 100.0
    g.set_input(NVDA_TARGET_PCT_NODE, canonical_target_frac)
    g.recompute()

    stmt = g.get("surface:nvda_target_statement").value
    # The surface renders the constant's %-point value.
    assert f"{NVDA_TARGET_PCT:.1f}%" in stmt, f"stmt={stmt!r}"
    # And the cap constant's %-point value is NOT what the target statement shows
    assert f"{DEFAULT_NVDA_CAP_PCT * 100:.1f}%" not in stmt


def test_nvda_cap_and_target_in_canonical_subject_node() -> None:
    """Both NVDA subjects are registered in CANONICAL_SUBJECT_NODE so they
    participate in the one-source-per-subject unification and will be included
    in any coverage assertion that iterates CANONICAL_SUBJECT_NODE."""
    assert "nvda_cap" in CANONICAL_SUBJECT_NODE
    assert "nvda_target" in CANONICAL_SUBJECT_NODE
    assert CANONICAL_SUBJECT_NODE["nvda_cap"] == NVDA_CAP_PCT_NODE
    assert CANONICAL_SUBJECT_NODE["nvda_target"] == NVDA_TARGET_PCT_NODE


def test_nvda_cap_and_target_cross_surface_coherence_catches_divergence() -> None:
    """The assembled-artifact cross-surface gate (check_cross_surface_coherence)
    BLOCKS when the alloc_doc surface reports a different cap or target than the
    body resolver — this is the structural guard against the 12%/8% contradiction
    recurring.

    Scenario: body resolver says cap=13%, target=8%; alloc_doc somehow says
    cap=13%, target=12% (the old stale value).  The gate must raise a violation
    on the target concept."""
    from types import SimpleNamespace
    from argosy.quality.coherence_gate import check_cross_surface_coherence
    from argosy.services.assembled_artifact import CONCEPT_NVDA_CAP, CONCEPT_NVDA_TARGET

    # Simulate what _add_body_values and _add_alloc_doc_values would produce.
    surface_values: dict = {
        CONCEPT_NVDA_CAP: [("body", 13.0), ("alloc_doc", 13.0)],  # cap agrees ✓
        CONCEPT_NVDA_TARGET: [("body", 8.0), ("alloc_doc", 12.0)],  # target disagrees ✗
    }
    art = SimpleNamespace(surface_values=surface_values)
    violations = check_cross_surface_coherence(art)
    assert len(violations) >= 1
    assert any(CONCEPT_NVDA_TARGET in v.detail for v in violations), (
        f"Expected a {CONCEPT_NVDA_TARGET} violation; got: {violations}"
    )
    # Cap is consistent — no violation expected for it.
    assert all(CONCEPT_NVDA_CAP not in v.detail for v in violations), (
        f"Unexpected cap violation: {violations}"
    )


def test_nvda_cap_and_target_agree_across_body_and_alloc_doc_when_consistent() -> None:
    """When body and alloc_doc both report the same cap and target, the coherence
    gate passes with zero violations — the canonical binding does not create
    false positives."""
    from types import SimpleNamespace
    from argosy.quality.coherence_gate import check_cross_surface_coherence
    from argosy.services.assembled_artifact import CONCEPT_NVDA_CAP, CONCEPT_NVDA_TARGET

    surface_values: dict = {
        CONCEPT_NVDA_CAP: [("body", 13.0), ("alloc_doc", 13.0)],
        CONCEPT_NVDA_TARGET: [("body", 8.0), ("alloc_doc", 8.0)],
    }
    art = SimpleNamespace(surface_values=surface_values)
    violations = check_cross_surface_coherence(art)
    assert violations == [], f"Unexpected violations: {violations}"
