"""Tests for graph_to_plan — render plan-version fields FROM a derivation graph
(the inverse of graph_hydration.add_surface_nodes). Pure: no DB, no LLM."""
from __future__ import annotations

import json

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.graph_to_plan import render_sections_json_from_graph


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
