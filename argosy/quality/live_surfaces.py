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
FI_CROSSING_YEAR_NODE = "retirement.fi_crossing_year"      # reconciled trajectory crossing
EARLIEST_SAFE_AGE_NODE = "retirement.earliest_safe_age"     # the one honest age
NET_WORTH_LIQUID_NODE = "net_worth.liquid_nis"              # liquid basis, distinct
NET_WORTH_INVESTABLE_NODE = "net_worth.investable_nis"     # investable basis, distinct
NET_WORTH_TOTAL_NODE = "net_worth.total_incl_residence_nis"  # total basis, incl. residence
US_SITUS_ESTATE_NODE = "estate.us_situs_exposure_nis"      # US-situs estate exposure
RETENTION_AT_VEST_NODE = "tax.retention_at_vest_pct"            # at-vest ordinary income
RETENTION_CAPITAL_TRACK_NODE = "tax.retention_capital_track_pct"  # Section-102 capital track


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
    "net_worth_total": NET_WORTH_TOTAL_NODE,
    "us_situs_estate": US_SITUS_ESTATE_NODE,
    "fi_crossing": FI_CROSSING_YEAR_NODE,
    "retention_at_vest": RETENTION_AT_VEST_NODE,
    "retention_capital_track": RETENTION_CAPITAL_TRACK_NODE,
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


def _net_worth_total_surfaces(node_key: str) -> list[Node]:
    """Net-worth (TOTAL basis, incl. residence) surfaces — distinctly labelled
    'total (incl. residence)' so it is never confused with the liquid or
    investable basis (resolves the ₪14.05M-vs-₪11.87M dashboard contradiction)."""
    return [
        make_surface_node(
            key="surface:dashboard.net_worth_total_tile",
            inputs=(node_key,),
            recipe=lambda i: f"Net worth (total basis, incl. residence): ₪{i[node_key]:,.0f}",
            compute_version="nw-total-tile-v1",
        ),
        make_surface_node(
            key="surface:appendix.net_worth_total",
            inputs=(node_key,),
            recipe=lambda i: f"| Net worth — total (incl. residence) | ₪{i[node_key]:,.0f} |",
            compute_version="nw-total-appendix-v1",
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


def _fi_crossing_surfaces(node_key: str) -> list[Node]:
    """FI-crossing-year surface — the projected calendar year the current liquid
    net worth plus a real-savings annuity reaches the FI total-capital target.
    The value is reconciled with the FI margin at the resolver (and again on the
    seeded scalars in incremental_plan._reconcile_fi_crossing), so this surface
    can never show a past/present crossing while the FI verdict says 'not
    reached'. A non-positive / pre-2000 value is the fail-closed seed for a
    pending crossing and renders explicitly."""
    def _render(i: dict) -> str:
        yr = i[node_key]
        if yr and yr >= 2000:
            return (
                "Projected FI-capital crossing year (current liquid net worth + "
                f"real-savings trajectory): {int(yr)}."
            )
        return "FI-capital crossing year: not reached within the projection horizon."

    return [
        make_surface_node(
            key="surface:fi_crossing_statement",
            inputs=(node_key,),
            recipe=_render,
            compute_version="fi-crossing-v1",
        ),
        make_surface_node(
            key="surface:dashboard.fi_crossing_tile",
            inputs=(node_key,),
            recipe=lambda i: (
                f"FI crossing: {int(i[node_key])}"
                if i[node_key] and i[node_key] >= 2000
                else "FI crossing: beyond horizon"
            ),
            compute_version="fi-crossing-tile-v1",
        ),
    ]


def _retention_pct_or_pending(value, label: str) -> str:
    """Render a retention rate ONLY when it is a valid fraction in (0, 1]; the
    fail-closed 0.0 seed (a pending/omitted resolver value) and any out-of-range
    value render an explicit pending string, never a false '0%' rate. A true 0%
    retention is not a legitimate statutory value here, so 0.0 is unambiguously
    the pending sentinel."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 < value <= 1.0:
        return f"{label}: {value * 100:.0f}%"
    return f"{label}: [derivation pending]"


def _retention_at_vest_surfaces(node_key: str) -> list[Node]:
    """At-vest RSU income retention (ordinary income — top marginal + surtax).
    Distinctly labelled 'at-vest (ordinary)' so it is never conflated with the
    capital-track rate."""
    return [
        make_surface_node(
            key="surface:retention_at_vest_statement",
            inputs=(node_key,),
            recipe=lambda i: _retention_pct_or_pending(
                i[node_key],
                "RSU net retention — at-vest (ordinary income, top marginal + surtax)"),
            compute_version="retention-at-vest-v1",
        ),
    ]


def _retention_capital_track_surfaces(node_key: str) -> list[Node]:
    """Capital-track RSU retention (Section-102 capital-gain slice — CGT + surtax).
    Distinctly labelled so it is never conflated with the at-vest ordinary rate."""
    return [
        make_surface_node(
            key="surface:retention_capital_track_statement",
            inputs=(node_key,),
            recipe=lambda i: _retention_pct_or_pending(
                i[node_key],
                "RSU net retention — capital-track (Section 102 capital-gain slice, CGT + surtax)"),
            compute_version="retention-capital-track-v1",
        ),
    ]


# subject_type -> (canonical derived node key default, surfaces builder). The
# default node key is overridable via the subject->node_key map argument.
_SUBJECT_BUILDERS: dict[str, SubjectSurfaceBuilder] = {
    "fi_capital_sufficiency": _fi_sufficiency_surfaces,
    "retirement_age_headline": _retirement_age_surfaces,
    "net_worth_liquid": _net_worth_liquid_surfaces,
    "net_worth_investable": _net_worth_investable_surfaces,
    "net_worth_total": _net_worth_total_surfaces,
    "us_situs_estate": _us_situs_estate_surfaces,
    "fi_crossing": _fi_crossing_surfaces,
    "retention_at_vest": _retention_at_vest_surfaces,
    "retention_capital_track": _retention_capital_track_surfaces,
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


def canonical_surface_concepts(
    subject_node_map: dict[str, str] | None = None,
) -> dict[str, list[SurfaceConcept]]:
    """The surface->concepts map (for surface_rendering.register_surface_concepts
    / the coherence recheck): every surface of a subject asserts that subject's
    concept, bound to the ONE canonical derived node. Because all surfaces of a
    subject point at the same value_input_key, the coherence gate sees identical
    values — the canonical render and the coherence view agree.

    ``subject_node_map`` overrides the default ``CANONICAL_SUBJECT_NODE`` mapping
    EXACTLY as ``register_canonical_surfaces`` does, so the concept binds to the
    node key the surfaces actually render from (e.g. the hydrated resolver key),
    not an absent default — otherwise the coherence recheck silently skips every
    overridden subject."""
    resolved = dict(CANONICAL_SUBJECT_NODE)
    if subject_node_map:
        resolved.update(subject_node_map)
    out: dict[str, list[SurfaceConcept]] = {}
    for subject_type, builder in _SUBJECT_BUILDERS.items():
        node_key = resolved[subject_type]
        for s in builder(node_key):
            out[s.key] = [SurfaceConcept(concept=subject_type, value_input_key=node_key)]
    return out


__all__ = [
    "FI_MARGIN_NODE",
    "EARLIEST_SAFE_AGE_NODE",
    "FI_CROSSING_YEAR_NODE",
    "NET_WORTH_LIQUID_NODE",
    "NET_WORTH_INVESTABLE_NODE",
    "NET_WORTH_TOTAL_NODE",
    "US_SITUS_ESTATE_NODE",
    "RETENTION_AT_VEST_NODE",
    "RETENTION_CAPITAL_TRACK_NODE",
    "CANONICAL_SUBJECT_NODE",
    "CanonicalRegistration",
    "register_canonical_surfaces",
    "canonical_surface_concepts",
]
