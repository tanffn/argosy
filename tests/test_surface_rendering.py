import pytest

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.surface_rendering import (
    SurfaceRenderError,
    make_surface_node,
    render_fi_verdict_text,
    build_fi_margin_surfaces,
    SurfaceConcept,
    register_surface_concepts,
    extract_surface_values,
    recheck_coherence,
    propagate_and_recheck,
    reconcile_prose_surface,
)


def test_make_surface_node_builds_a_surface_kind_node():
    node = make_surface_node(
        key="surface:fi_tile",
        inputs=("retirement.fi_margin_signed_nis",),
        recipe=lambda inbound: f"margin {inbound['retirement.fi_margin_signed_nis']}",
        compute_version="tile-v1",
    )
    assert node.kind is NodeKind.SURFACE
    assert node.key == "surface:fi_tile"
    assert node.inputs == ("retirement.fi_margin_signed_nis",)
    assert node.compute_version == "tile-v1"


def test_surface_node_renders_from_its_inbound_value():
    g = DerivationGraph()
    g.add_node(Node(key="retirement.fi_margin_signed_nis", kind=NodeKind.INPUT, value=-148_208.0))
    g.add_node(make_surface_node(
        key="surface:fi_tile",
        inputs=("retirement.fi_margin_signed_nis",),
        recipe=lambda inbound: f"margin {inbound['retirement.fi_margin_signed_nis']:.0f}",
        compute_version="tile-v1",
    ))
    g.recompute()
    assert g.get("surface:fi_tile").value == "margin -148208"


def test_make_surface_node_rejects_a_non_callable_recipe():
    with pytest.raises(SurfaceRenderError):
        make_surface_node(key="surface:x", inputs=("a",), recipe=None, compute_version="v1")


def _fi_graph(margin_value: float) -> DerivationGraph:
    """A graph where ONE fi_margin DERIVED node feeds three surfaces."""
    g = DerivationGraph()
    # Inputs the margin derives from.
    g.add_node(Node(key="portfolio.liquid_net_worth_nis", kind=NodeKind.INPUT, value=11_687_926.0))
    g.add_node(Node(key="retirement.fi_total_capital_nis", kind=NodeKind.INPUT,
                    value=11_687_926.0 - margin_value))
    # The ONE derived margin node (liquid_nw - total_capital).
    g.add_node(Node(
        key="retirement.fi_margin_signed_nis",
        kind=NodeKind.DERIVED,
        inputs=("portfolio.liquid_net_worth_nis", "retirement.fi_total_capital_nis"),
        recipe=lambda i: i["portfolio.liquid_net_worth_nis"] - i["retirement.fi_total_capital_nis"],
        compute_version="fi-margin-v1",
    ))
    for node in build_fi_margin_surfaces():
        g.add_node(node)
    g.recompute()
    return g


def test_one_margin_node_feeds_all_surfaces_identically_when_short():
    g = _fi_graph(margin_value=-148_208.0)
    tile = g.get("surface:dashboard.fi_tile").value
    table = g.get("surface:appendix.fi_table").value
    verdict = g.get("surface:fi_verdict").value
    # Every surface reads the SAME margin: all say "short", none says "reached".
    assert "short" in verdict.lower()
    assert "reached" not in verdict.lower() or "not" in verdict.lower()
    assert "148,208" in tile or "148208" in tile
    assert "148,208" in table or "148208" in table
    assert "148,208" in verdict or "148208" in verdict


def test_changing_the_input_updates_all_surfaces_with_no_basis_flip():
    g = _fi_graph(margin_value=-148_208.0)
    # Flip the input so the margin becomes POSITIVE (FI reached).
    invalidated = g.set_input("retirement.fi_total_capital_nis", 11_000_000.0)
    # The derived margin + all three surfaces are downstream of the input.
    assert "retirement.fi_margin_signed_nis" in invalidated
    assert {"surface:dashboard.fi_tile", "surface:appendix.fi_table",
            "surface:fi_verdict"} <= invalidated
    g.recompute()
    margin = g.get("retirement.fi_margin_signed_nis").value
    assert margin == pytest.approx(687_926.0)
    # ALL surfaces now say reached — none stuck on "short" (the basis-flip bug).
    verdict = g.get("surface:fi_verdict").value
    tile = g.get("surface:dashboard.fi_tile").value
    table = g.get("surface:appendix.fi_table").value
    assert "reached" in verdict.lower() and "short" not in verdict.lower()
    assert "687,926" in tile or "687926" in tile
    assert "687,926" in table or "687926" in table


def test_render_fi_verdict_text_matches_resolver_doctrine():
    # Negative margin -> NOT reached, states the shortfall amount.
    assert "short" in render_fi_verdict_text(-148_208.0).lower()
    # Positive margin -> REACHED, states the margin amount.
    assert "reached" in render_fi_verdict_text(687_926.0).lower()


def test_extract_surface_values_groups_by_concept():
    g = _fi_graph(margin_value=-148_208.0)
    # Declare that all three FI surfaces assert the SAME concept = the margin.
    register_surface_concepts({
        "surface:dashboard.fi_tile": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
        "surface:appendix.fi_table": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
        "surface:fi_verdict": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
    })
    sv = extract_surface_values(g)
    pairs = dict(sv["fi_margin"])
    # Every surface reports the SAME margin value (no divergence).
    assert pairs["surface:dashboard.fi_tile"] == pytest.approx(-148_208.0)
    assert pairs["surface:appendix.fi_table"] == pytest.approx(-148_208.0)
    assert pairs["surface:fi_verdict"] == pytest.approx(-148_208.0)


def test_extract_surface_values_ignores_surfaces_with_no_declared_concept():
    g = _fi_graph(margin_value=-148_208.0)
    register_surface_concepts({})  # nothing declared
    assert extract_surface_values(g) == {}


def test_recheck_coherence_passes_when_surfaces_agree():
    g = _fi_graph(margin_value=-148_208.0)
    register_surface_concepts({
        "surface:dashboard.fi_tile": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
        "surface:fi_verdict": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
    })
    violations = recheck_coherence(g)
    assert violations == []  # both surfaces read the one node -> agree


def test_recheck_coherence_flags_a_planted_divergence():
    # Two surfaces declared to assert the SAME concept but bound to DIFFERENT
    # nodes with sign-flipped values -> the gate must flag it (the basis-flip the
    # graph design eliminates, here forced to prove the recheck catches it).
    g = _fi_graph(margin_value=-148_208.0)
    g.add_node(Node(key="bad.positive_margin", kind=NodeKind.INPUT, value=118_020.0))
    register_surface_concepts({
        "surface:fi_verdict": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
        "surface:dashboard.fi_tile": [SurfaceConcept("fi_margin", "bad.positive_margin")],
    })
    violations = recheck_coherence(g)
    assert len(violations) == 1
    assert "fi_margin" in violations[0].detail
    assert "SIGN FLIP" in violations[0].detail


def test_propagate_recomputes_only_the_blast_radius_and_rechecks():
    g = _fi_graph(margin_value=-148_208.0)
    # An INDEPENDENT surface not downstream of the margin must NOT re-render.
    g.add_node(Node(key="indep.value", kind=NodeKind.INPUT, value=42.0))
    g.add_node(make_surface_node(
        key="surface:indep_tile",
        inputs=("indep.value",),
        recipe=lambda i: f"indep {i['indep.value']:.0f}",
        compute_version="indep-v1",
    ))
    g.recompute()
    before_indep = g.get("surface:indep_tile").value
    register_surface_concepts({
        "surface:fi_verdict": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
        "surface:dashboard.fi_tile": [SurfaceConcept("fi_margin", "retirement.fi_margin_signed_nis")],
    })

    result = propagate_and_recheck(g, input_key="retirement.fi_total_capital_nis", value=11_000_000.0)

    # The margin + its three surfaces recomputed; the independent surface did not.
    assert "retirement.fi_margin_signed_nis" in result.recomputed
    assert "surface:fi_verdict" in result.recomputed
    assert "surface:indep_tile" not in result.recomputed
    assert g.get("surface:indep_tile").value == before_indep  # byte-identical
    # Coherence recheck ran and passed (surfaces still agree).
    assert result.coherence_violations == []
    assert "reached" in g.get("surface:fi_verdict").value.lower()


def test_propagate_rejects_a_non_input_target():
    g = _fi_graph(margin_value=-148_208.0)
    with pytest.raises(ValueError):
        # The signed margin is DERIVED — not directly settable (derive-don't-inherit).
        propagate_and_recheck(g, input_key="retirement.fi_margin_signed_nis", value=1.0)


class _Finding:
    def __init__(self, kind, detail, surfaces_cited):
        self.kind = kind
        self.detail = detail
        self.surfaces_cited = surfaces_cited


class _Verdict:
    def __init__(self, findings):
        self.findings = findings


def test_prose_surface_edit_is_span_local():
    bodies = {
        "long": "Intro stays. FI is reached today. Outro also stays.",
        "medium": "",
        "short": "",
    }
    verdict = _Verdict([
        _Finding("contradiction", "FI is not reached on the liquid basis",
                 ["FI is reached today"]),
    ])
    # Stub editor: corrects ONLY the cited span, introduces NO new number.
    result = reconcile_prose_surface(
        bodies=bodies,
        reader_verdict=verdict,
        resolved=None,
        editor=lambda prompt: "FI is not yet reached on the liquid basis",
    )
    corrected = result.corrected_bodies["long"]
    assert "FI is not yet reached on the liquid basis" in corrected
    assert corrected.startswith("Intro stays.")   # before-span byte-identical
    assert corrected.endswith("Outro also stays.")  # after-span byte-identical
    assert len(result.edits) == 1


def test_prose_surface_edit_rejects_a_new_number():
    bodies = {"long": "FI is reached today.", "medium": "", "short": ""}
    verdict = _Verdict([
        _Finding("contradiction", "wrong", ["FI is reached today"]),
    ])
    # The stub tries to inject a fabricated ₪999 — must be rejected (no-new-numbers).
    result = reconcile_prose_surface(
        bodies=bodies,
        reader_verdict=verdict,
        resolved=None,
        editor=lambda prompt: "FI short by 999",
    )
    assert result.corrected_bodies["long"] == "FI is reached today."  # unchanged
    assert result.edits == []


def test_public_exports():
    import argosy.quality.surface_rendering as sr
    for name in (
        "SurfaceRenderError", "make_surface_node", "render_fi_verdict_text",
        "build_fi_margin_surfaces", "SurfaceConcept", "register_surface_concepts",
        "extract_surface_values", "recheck_coherence", "PropagationResult",
        "propagate_and_recheck", "reconcile_prose_surface",
    ):
        assert name in sr.__all__
