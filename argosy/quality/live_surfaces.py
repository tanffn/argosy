"""Canonical live surfaces — every contradiction-prone subject renders from ONE
shared DERIVED node, so two surfaces CANNOT show different values/bases.

The root cause of the recurring cross-surface contradictions (the FI tile saying
"reached" while the appendix says "short ₪X"; the headline retirement age 46 vs a
dashboard age of 47; net worth quoted on a liquid basis in one place and an
investable basis in another) is that each surface computed its own number. Here
we bind every such subject to a SINGLE ``NodeKind.DERIVED`` node and build all of
its surfaces (headline, dashboard tile, appendix row, verdict) as
``NodeKind.SURFACE`` nodes whose ONLY inbound edge is that derived node. Because
the engine re-renders every stale surface from the same value on ``recompute``,
the surfaces are identical by construction — a basis-flip or an age-divergence is
impossible.

``CANONICAL_SUBJECT_NODE`` is the unification: it maps each coherence
``surface_registry.SUBJECT_REGISTRY`` subject_type that this module canonicalizes
to its ONE derived node key — one source of truth per subject.

See docs/superpowers/specs/2026-06-18-living-plan-derivation-graph-design.md
(Layer 3 — canonical single-node render).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.surface_rendering import (
    SurfaceConcept,
    make_surface_node,
    render_fi_verdict_text,
)

# --- Canonical derived-node keys (one per contradiction-prone subject) --------
# Each is the SINGLE NodeKind.DERIVED node that every surface for that subject
# renders from. Surfaces never carry their own copy of the number.
FI_MARGIN_NODE = "retirement.fi_margin_signed_nis"          # liquid − total capital target
EARLIEST_SAFE_AGE_NODE = "retirement.earliest_safe_age"     # the one honest age
NET_WORTH_LIQUID_NODE = "net_worth.liquid_nis"              # liquid basis, distinct
NET_WORTH_INVESTABLE_NODE = "net_worth.investable_nis"     # investable basis, distinct
US_SITUS_ESTATE_NODE = "estate.us_situs_exposure_nis"      # US-situs estate exposure


# --- The unification: coherence subject_type -> its ONE canonical derived node -
# Keys are exactly the SUBJECT_REGISTRY subject_types this module canonicalizes.
# Net worth has two distinct, separately-labelled bases, each its own node — they
# are NOT the same number, so they map to different keys (the "distinctly
# labelled" requirement). The liquid/investable distinction is in the node key
# itself, never collapsed into one ambiguous "net worth".
CANONICAL_SUBJECT_NODE: dict[str, str] = {
    "fi_capital_sufficiency": FI_MARGIN_NODE,
    "retirement_age_headline": EARLIEST_SAFE_AGE_NODE,
    "net_worth_liquid": NET_WORTH_LIQUID_NODE,
    "net_worth_investable": NET_WORTH_INVESTABLE_NODE,
    "us_situs_estate": US_SITUS_ESTATE_NODE,
}


# A builder takes the canonical derived node key and returns the SURFACE nodes
# (headline + dashboard tile + appendix, etc.) that all render from it.
SubjectSurfaceBuilder = Callable[[str], list[Node]]


def _fi_sufficiency_surfaces(node_key: str) -> list[Node]:
    """FI-sufficiency surfaces — verdict + dashboard tile + appendix row, ALL
    from the one signed-margin node. The verdict word is a pure function of the
    margin's sign (render_fi_verdict_text), so the tile and the verdict cannot
    disagree about reached-vs-short (kills the FI basis-flip)."""
    return [
        make_surface_node(
            key="surface:fi_verdict",
            inputs=(node_key,),
            recipe=lambda i: render_fi_verdict_text(i[node_key]),
            compute_version="fi-verdict-v1",
        ),
        make_surface_node(
            key="surface:dashboard.fi_tile",
            inputs=(node_key,),
            recipe=lambda i: render_fi_verdict_text(i[node_key]),
            compute_version="fi-tile-v1",
        ),
        make_surface_node(
            key="surface:appendix.fi_table",
            inputs=(node_key,),
            recipe=lambda i: (
                "| Concept | Value |\n"
                "| --- | --- |\n"
                f"| FI sufficiency margin (liquid − total capital) | ₪{i[node_key]:,.0f} |"
            ),
            compute_version="fi-table-v1",
        ),
    ]


def _retirement_age_surfaces(node_key: str) -> list[Node]:
    """Retirement-age surfaces — the headline and the dashboard age tile, ALL
    from the one earliest_safe_age node. Identical age by construction (kills the
    46-vs-dashboard divergence)."""
    return [
        make_surface_node(
            key="surface:retirement_age_headline",
            inputs=(node_key,),
            recipe=lambda i: f"Earliest safe retirement age: {int(i[node_key])}.",
            compute_version="age-headline-v1",
        ),
        make_surface_node(
            key="surface:dashboard.age_tile",
            inputs=(node_key,),
            recipe=lambda i: f"Earliest safe age: {int(i[node_key])}",
            compute_version="age-tile-v1",
        ),
    ]


def _net_worth_liquid_surfaces(node_key: str) -> list[Node]:
    """Net-worth (LIQUID basis) surfaces — distinctly labelled 'liquid' so it is
    never confused with the investable basis."""
    return [
        make_surface_node(
            key="surface:dashboard.net_worth_liquid_tile",
            inputs=(node_key,),
            recipe=lambda i: f"Net worth (liquid basis): ₪{i[node_key]:,.0f}",
            compute_version="nw-liquid-tile-v1",
        ),
        make_surface_node(
            key="surface:appendix.net_worth_liquid",
            inputs=(node_key,),
            recipe=lambda i: f"| Net worth — liquid | ₪{i[node_key]:,.0f} |",
            compute_version="nw-liquid-appendix-v1",
        ),
    ]


def _net_worth_investable_surfaces(node_key: str) -> list[Node]:
    """Net-worth (INVESTABLE basis) surfaces — distinctly labelled 'investable'."""
    return [
        make_surface_node(
            key="surface:dashboard.net_worth_investable_tile",
            inputs=(node_key,),
            recipe=lambda i: f"Net worth (investable basis): ₪{i[node_key]:,.0f}",
            compute_version="nw-investable-tile-v1",
        ),
        make_surface_node(
            key="surface:appendix.net_worth_investable",
            inputs=(node_key,),
            recipe=lambda i: f"| Net worth — investable | ₪{i[node_key]:,.0f} |",
            compute_version="nw-investable-appendix-v1",
        ),
    ]


def _us_situs_estate_surfaces(node_key: str) -> list[Node]:
    """US-situs estate-exposure surfaces — headline + dashboard tile, ALL from the
    one exposure node."""
    return [
        make_surface_node(
            key="surface:us_situs_estate_headline",
            inputs=(node_key,),
            recipe=lambda i: f"US-situs estate exposure: ₪{i[node_key]:,.0f}.",
            compute_version="us-situs-headline-v1",
        ),
        make_surface_node(
            key="surface:dashboard.us_situs_estate_tile",
            inputs=(node_key,),
            recipe=lambda i: f"US-situs exposure: ₪{i[node_key]:,.0f}",
            compute_version="us-situs-tile-v1",
        ),
    ]


# subject_type -> (canonical derived node key default, surfaces builder). The
# default node key is overridable via the subject->node_key map argument.
_SUBJECT_BUILDERS: dict[str, SubjectSurfaceBuilder] = {
    "fi_capital_sufficiency": _fi_sufficiency_surfaces,
    "retirement_age_headline": _retirement_age_surfaces,
    "net_worth_liquid": _net_worth_liquid_surfaces,
    "net_worth_investable": _net_worth_investable_surfaces,
    "us_situs_estate": _us_situs_estate_surfaces,
}


@dataclass(frozen=True)
class CanonicalRegistration:
    """The result of registering one subject's canonical surfaces: the subject,
    the one derived node it renders from, and the surface ids built over it."""

    subject_type: str
    node_key: str
    surface_ids: tuple[str, ...]


def register_canonical_surfaces(
    graph: DerivationGraph,
    subject_node_map: dict[str, str] | None = None,
) -> list[CanonicalRegistration]:
    """Register, for every canonicalized subject, the SURFACE nodes that ALL
    render from that subject's ONE derived node. ``subject_node_map`` overrides
    the default ``CANONICAL_SUBJECT_NODE`` mapping (e.g. to point a subject at a
    hydrated node key); subjects absent from the map fall back to the default.

    Assumes the derived node for each subject already exists in ``graph`` (built
    by graph_hydration); this function only adds the SURFACE consumers. Returns
    one CanonicalRegistration per subject so a caller (and the coverage test) can
    assert exactly-one-node-per-subject.
    """
    resolved = dict(CANONICAL_SUBJECT_NODE)
    if subject_node_map:
        resolved.update(subject_node_map)

    out: list[CanonicalRegistration] = []
    for subject_type, builder in _SUBJECT_BUILDERS.items():
        node_key = resolved[subject_type]
        surfaces = builder(node_key)
        surface_ids: list[str] = []
        for s in surfaces:
            graph.add_node(s)
            surface_ids.append(s.key)
        out.append(
            CanonicalRegistration(
                subject_type=subject_type,
                node_key=node_key,
                surface_ids=tuple(surface_ids),
            )
        )
    return out


def canonical_surface_concepts() -> dict[str, list[SurfaceConcept]]:
    """The surface->concepts map (for surface_rendering.register_surface_concepts
    / the coherence recheck): every surface of a subject asserts that subject's
    concept, bound to the ONE canonical derived node. Because all surfaces of a
    subject point at the same value_input_key, the coherence gate sees identical
    values — the canonical render and the coherence view agree."""
    out: dict[str, list[SurfaceConcept]] = {}
    for subject_type, builder in _SUBJECT_BUILDERS.items():
        node_key = CANONICAL_SUBJECT_NODE[subject_type]
        for s in builder(node_key):
            out[s.key] = [SurfaceConcept(concept=subject_type, value_input_key=node_key)]
    return out


__all__ = [
    "FI_MARGIN_NODE",
    "EARLIEST_SAFE_AGE_NODE",
    "NET_WORTH_LIQUID_NODE",
    "NET_WORTH_INVESTABLE_NODE",
    "US_SITUS_ESTATE_NODE",
    "CANONICAL_SUBJECT_NODE",
    "CanonicalRegistration",
    "register_canonical_surfaces",
    "canonical_surface_concepts",
]
