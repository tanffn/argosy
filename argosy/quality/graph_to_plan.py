"""Render plan-version fields FROM a derivation graph — the inverse of
``graph_hydration.add_surface_nodes``.

Surface nodes are keyed ``surface:<horizon>:<section_id>`` and carry the section's
(possibly edited) ``body_md``. This module walks a base section list and replaces
each body with the graph's CURRENT valid surface value, so the incremental path
can emit a publishable plan artifact. Fail-safe: when a surface is absent or
invalid, the base body is kept — an un-derived edit is never emitted.

Pure: no DB, no LLM. The first piece of the generator-swap render bridge (M1).
"""
from __future__ import annotations

import json
from typing import Any

from argosy.quality.derivation_graph import DerivationGraph


def _surface_key(horizon: str, section_id: str) -> str:
    return f"surface:{horizon}:{section_id}"


def render_sections_json_from_graph(
    graph: DerivationGraph, base_sections: list[dict[str, Any]]
) -> str:
    """Return sections_json (a JSON list) with each section's ``body_md`` replaced
    by the graph's VALID surface-node value; the base body is kept when the
    surface is absent or invalid (fail-safe — never emit an un-derived edit).
    All non-body fields (title, evidence, …) are preserved verbatim."""
    keys = set(graph.keys())
    out: list[dict[str, Any]] = []
    for sec in base_sections:
        s = dict(sec)
        k = _surface_key(str(sec.get("horizon", "")), str(sec.get("section_id", "")))
        if k in keys and graph.is_valid(k):
            val = graph.get(k).value
            if isinstance(val, str) and val:
                s["body_md"] = val
        out.append(s)
    return json.dumps(out, ensure_ascii=False)


_HORIZONS = ("long", "medium", "short")


def render_plan_fields_from_graph(
    graph: DerivationGraph, base_sections: list[dict[str, Any]]
) -> dict[str, str]:
    """Assemble the full plan-version field dict from the graph:

      * ``sections_json`` — every section with graph-overridden bodies.
      * ``horizon_<h>_md`` for h in long/medium/short — that horizon's section
        bodies (in section order) joined by a blank line.

    Bodies come from :func:`render_sections_json_from_graph`, so the same
    fail-safe applies (base body kept when the surface is absent/invalid)."""
    sections_json = render_sections_json_from_graph(graph, base_sections)
    rendered: list[dict[str, Any]] = json.loads(sections_json)
    fields: dict[str, str] = {"sections_json": sections_json}
    for h in _HORIZONS:
        bodies = [
            str(s.get("body_md", ""))
            for s in rendered
            if str(s.get("horizon", "")) == h
        ]
        fields[f"horizon_{h}_md"] = "\n\n".join(b for b in bodies if b)
    return fields


__all__ = ["render_sections_json_from_graph", "render_plan_fields_from_graph"]
