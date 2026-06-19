"""Tests for graph_to_plan — render plan-version fields FROM a derivation graph
(the inverse of graph_hydration.add_surface_nodes). Pure: no DB, no LLM."""
from __future__ import annotations

import json

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.graph_to_plan import (
    render_plan_fields_from_graph,
    render_sections_json_from_graph,
)


def _section(section_id, horizon, body):
    return {"section_id": section_id, "horizon": horizon, "title": "t",
            "body_md": body, "evidence": {"source_span": []}}


def test_render_uses_graph_body_for_each_surface():
    base = [_section("posture", "long", "OLD body"),
            _section("vest", "medium", "OLD vest")]
    g = DerivationGraph()
    for key, val in [("surface:long:posture", "NEW body"),
                     ("surface:medium:vest", "NEW vest")]:
        g.add_node(Node(key=key, kind=NodeKind.SURFACE, value=val))
        # Make the node VALID (input_hash matches) as it would be post-recompute.
        g.get(key).input_hash = g.hash_of(key)
    out = json.loads(render_sections_json_from_graph(g, base))
    bodies = {(s["section_id"], s["horizon"]): s["body_md"] for s in out}
    assert bodies[("posture", "long")] == "NEW body"
    assert bodies[("vest", "medium")] == "NEW vest"
    # non-body fields preserved
    assert out[0]["title"] == "t"


def test_render_keeps_base_body_when_surface_absent_or_invalid():
    base = [_section("posture", "long", "BASE body")]
    g = DerivationGraph()  # no surface node for it
    out = json.loads(render_sections_json_from_graph(g, base))
    assert out[0]["body_md"] == "BASE body"


def test_roundtrip_hydrate_then_render_preserves_bodies():
    """hydrate(plan)->graph->render reproduces every section body unchanged — the
    'verify, not just reproduce' foundation. Uses duck-typed sections (the same
    attributes add_surface_nodes reads) to keep the round-trip pure."""
    from types import SimpleNamespace

    from argosy.quality.graph_hydration import add_surface_nodes, recompute_safe

    def _cite(loc):
        return SimpleNamespace(source_locator=loc)

    secs = [
        SimpleNamespace(section_id="posture", horizon="long",
                        body_md="long posture body",
                        evidence=SimpleNamespace(source_span=[_cite("x")])),
        SimpleNamespace(section_id="vest", horizon="medium",
                        body_md="medium vest body",
                        evidence=SimpleNamespace(source_span=[_cite("y")])),
    ]
    g = DerivationGraph()
    add_surface_nodes(g, secs)
    recompute_safe(g)

    base = [_section("posture", "long", "STALE"), _section("vest", "medium", "STALE")]
    out = json.loads(render_sections_json_from_graph(g, base))
    bodies = {(s["section_id"], s["horizon"]): s["body_md"] for s in out}
    assert bodies[("posture", "long")] == "long posture body"
    assert bodies[("vest", "medium")] == "medium vest body"


def test_render_plan_fields_assembles_horizon_markdown():
    base = [
        _section("posture", "long", "long body A"),
        _section("glide", "long", "long body B"),
        _section("vest", "medium", "medium body"),
        _section("park", "short", "short body"),
    ]
    g = DerivationGraph()
    for key, val in [("surface:long:posture", "LONG A"),
                     ("surface:long:glide", "LONG B")]:
        g.add_node(Node(key=key, kind=NodeKind.SURFACE, value=val))
        g.get(key).input_hash = g.hash_of(key)
    fields = render_plan_fields_from_graph(g, base)
    assert set(fields) >= {"sections_json", "horizon_long_md",
                           "horizon_medium_md", "horizon_short_md"}
    # Long horizon md = both long bodies (graph-overridden), in order, joined.
    assert fields["horizon_long_md"] == "LONG A\n\nLONG B"
    # Medium/short keep base bodies (no surface override).
    assert fields["horizon_medium_md"] == "medium body"
    assert fields["horizon_short_md"] == "short body"
    # Long md must NOT contain the medium/short bodies.
    assert "medium body" not in fields["horizon_long_md"]


def test_render_plan_fields_applies_prose_reconcile_seam():
    """The render bridge accepts an injectable prose-reconcile step (M2 plugs the
    real reader-driven surgical reconcile here). Deterministic fake: replace a
    stale figure with the canonical one. Proves prose is reconciled BEFORE the
    horizon markdown is assembled (so numbers in prose match canonical)."""
    base = [_section("posture", "long", "FI target is short ₪OLD of the goal.")]
    g = DerivationGraph()  # no surface override; reconcile does the edit

    def _reconcile(sections):
        out = []
        for s in sections:
            s = dict(s)
            s["body_md"] = s["body_md"].replace("₪OLD", "₪148,208")
            out.append(s)
        return out

    fields = render_plan_fields_from_graph(g, base, reconcile=_reconcile)
    assert "₪148,208" in fields["horizon_long_md"]
    assert "₪OLD" not in fields["horizon_long_md"]
    assert "₪148,208" in fields["sections_json"]
