"""Phase 1d — render SURFACE nodes from their inbound derived-value nodes.

A deterministic-render surface (dashboard tile, appendix table, the FI verdict)
is a ``NodeKind.SURFACE`` node whose ``recipe(inbound) -> str`` formats the
values of its inbound DERIVED-value nodes into text. Because N surfaces over the
SAME derived node all read one value, recomputing the graph after a
``set_input`` re-renders ALL of them identically — two surfaces CANNOT disagree
about the same fact (a basis-flip is impossible by construction).

Free-text prose surfaces do NOT re-render from scratch; they reuse the existing
converge-safe ``surgical_reconcile`` editor (span-local, forbidden from
introducing new numbers) driven by the prose surface node's recipe.

After a change is applied + propagated, a cheap deterministic coherence recheck
runs over the WHOLE rendered artifact (``coherence_gate.check_cross_surface_
coherence``) — the per-change backstop, NOT the promotion gate.

See docs/superpowers/specs/2026-06-18-living-plan-derivation-graph-design.md
(Layer 3 steps 4 + 6).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind

# A deterministic surface recipe maps {inbound_key: value} -> rendered text.
SurfaceRecipe = Callable[[dict[str, Any]], str]


class SurfaceRenderError(Exception):
    """Raised when a surface node is mis-declared (no recipe, no inputs)."""


def make_surface_node(
    *,
    key: str,
    inputs: tuple[str, ...],
    recipe: SurfaceRecipe | None,
    compute_version: str,
) -> Node:
    """Build a deterministic-render ``NodeKind.SURFACE`` node.

    The node renders by calling ``recipe`` with the {inbound_key: value} dict the
    engine assembles from ``inputs``. ``compute_version`` is the render-template
    version folded into the engine's ``input_hash`` so a template change
    invalidates the surface even when its inbound values are unchanged.
    """
    if recipe is None or not callable(recipe):
        raise SurfaceRenderError(f"surface {key!r} needs a callable recipe")
    if not inputs:
        raise SurfaceRenderError(f"surface {key!r} declares no inbound nodes")
    return Node(
        key=key,
        kind=NodeKind.SURFACE,
        inputs=tuple(inputs),
        recipe=recipe,
        compute_version=compute_version,
    )


def render_fi_verdict_text(margin_signed_nis: float) -> str:
    """The canonical FI-sufficiency VERDICT text, derived from the ONE signed
    margin. Mirrors plan_numeric_resolver.render_numbers_for_synth: >=0 => the
    liquid basis covers the total capital target (REACHED) with that margin;
    <0 => liquid is short of the target (NOT reached) by that amount. The verdict
    word is a pure function of the sign, so every surface that renders from this
    node states the SAME conclusion."""
    m = float(margin_signed_nis)
    if m >= 0:
        return (
            f"FI sufficiency VERDICT: REACHED — liquid net worth covers the total "
            f"capital target with a ₪{m:,.0f} margin."
        )
    return (
        f"FI sufficiency VERDICT: NOT reached — liquid net worth is short "
        f"₪{abs(m):,.0f} of the total capital target."
    )


def build_fi_margin_surfaces() -> list[Node]:
    """Three deterministic surfaces that ALL render from the ONE
    ``retirement.fi_margin_signed_nis`` derived node: the dashboard FI tile, the
    appendix FI table row, and the FI verdict. Because they share one inbound
    node, a change to the margin re-renders all three identically — a
    cross-surface basis-flip ('reached' on one, 'short' on another) is
    impossible by construction (spec Layer-3 step 4)."""
    margin_key = "retirement.fi_margin_signed_nis"
    return [
        make_surface_node(
            key="surface:dashboard.fi_tile",
            inputs=(margin_key,),
            recipe=lambda i: f"FI margin: ₪{i[margin_key]:,.0f}",
            compute_version="fi-tile-v1",
        ),
        make_surface_node(
            key="surface:appendix.fi_table",
            inputs=(margin_key,),
            recipe=lambda i: (
                "| Concept | Value |\n"
                "| --- | --- |\n"
                f"| FI sufficiency margin (liquid − total capital) | ₪{i[margin_key]:,.0f} |"
            ),
            compute_version="fi-table-v1",
        ),
        make_surface_node(
            key="surface:fi_verdict",
            inputs=(margin_key,),
            recipe=lambda i: render_fi_verdict_text(i[margin_key]),
            compute_version="fi-verdict-v1",
        ),
    ]


@dataclass(frozen=True)
class SurfaceConcept:
    """A concept a surface asserts, bound to the inbound node carrying its value.

    ``concept`` is the cross-surface coherence key; ``value_input_key`` is the
    inbound node whose (numeric) value the surface rendered for that concept.
    Reading the value off the bound node — not a regex over rendered text — keeps
    the extracted value EXACTLY the one the surface displayed."""

    concept: str
    value_input_key: str


# surface_id -> the concepts it asserts. Module-level so a test (and, later,
# hydration) can declare it; replaced wholesale by register_surface_concepts.
_SURFACE_CONCEPTS: dict[str, list[SurfaceConcept]] = {}


def register_surface_concepts(mapping: dict[str, list[SurfaceConcept]]) -> None:
    """Replace the surface->concepts registry (declarative, like
    coherence.surface_registry.SUBJECT_REGISTRY). Phase 1b/1c hydration supplies
    the real map; tests supply a focused one."""
    global _SURFACE_CONCEPTS
    _SURFACE_CONCEPTS = dict(mapping)


def extract_surface_values(graph: DerivationGraph) -> dict[str, list[tuple[str, float]]]:
    """Build the ``surface_values`` map coherence_gate expects:
    ``{concept: [(surface_id, numeric_value), ...]}`` for every SURFACE node that
    declares a concept. The value is read off the surface's bound inbound node,
    so it is the value that surface actually rendered. Non-numeric values are
    skipped (the coherence gate only compares numbers)."""
    out: dict[str, list[tuple[str, float]]] = {}
    for surface_id, concepts in _SURFACE_CONCEPTS.items():
        try:
            node = graph.get(surface_id)
        except Exception:  # noqa: BLE001 — a declared-but-absent surface is skipped
            continue
        if node.kind is not NodeKind.SURFACE:
            continue
        for c in concepts:
            try:
                val = graph.get(c.value_input_key).value
            except Exception:  # noqa: BLE001
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            out.setdefault(c.concept, []).append((surface_id, float(val)))
    return out


class _CoherenceView:
    """Minimal artifact shim: the only attribute check_cross_surface_coherence
    reads is ``surface_values``. Avoids constructing a full AssembledArtifact for
    the per-change recheck."""

    def __init__(self, surface_values: dict[str, list[tuple[str, float]]]) -> None:
        self.surface_values = surface_values


def recheck_coherence(graph: DerivationGraph):
    """The per-change CHEAP coherence backstop (spec Layer-3 step 6): run the
    deterministic cross-surface coherence gate over the WHOLE rendered artifact
    (every SURFACE node's declared concepts), returning its GateViolations. This
    is sub-second and runs every change; it is NOT the promotion gate (the full
    promote_gate authority set is the publish blocker, out of scope here)."""
    from argosy.quality.coherence_gate import check_cross_surface_coherence

    surface_values = extract_surface_values(graph)
    return check_cross_surface_coherence(_CoherenceView(surface_values))


@dataclass(frozen=True)
class PropagationResult:
    """The visible ripple of one change (spec Layer-5.2): what invalidated, what
    recomputed/re-rendered, and the per-change coherence verdict."""

    trigger_input: str
    invalidated: set[str]
    recomputed: list[str]
    coherence_violations: list


def propagate_and_recheck(graph: DerivationGraph, *, input_key: str, value: Any) -> PropagationResult:
    """Apply one input change, recompute the blast radius (re-rendering every
    stale DERIVED + SURFACE node deterministically), then run the cheap
    whole-artifact coherence recheck. Everything outside the closure is reused
    byte-identical (the engine only recomputes stale nodes). Raises ValueError if
    ``input_key`` is not an INPUT node (a DerivedValue is never hand-set)."""
    invalidated = graph.set_input(input_key, value)  # raises on a non-INPUT target
    recomputed = graph.recompute()
    violations = recheck_coherence(graph)
    return PropagationResult(
        trigger_input=input_key,
        invalidated=invalidated,
        recomputed=recomputed,
        coherence_violations=violations,
    )


def reconcile_prose_surface(
    *,
    bodies: dict,
    reader_verdict,
    resolved=None,
    editor: Callable[[str], str] | None = None,
):
    """Reconcile a free-text PROSE surface via the existing converge-safe
    surgical editor. This is the prose-surface render path in spec Layer-3 step
    4: instead of regenerating prose, we edit ONLY the reader's cited span
    (forbidden from introducing new numbers), so the fix converges and cannot
    reshuffle the rest of the document. Delegates to
    surgical_reconcile.surgically_correct_draft — Phase 1d reuses it verbatim
    rather than reimplementing span-local editing. ``editor`` is injectable for
    deterministic tests (no LLM/DB)."""
    from argosy.orchestrator.flows.plan_synthesis.surgical_reconcile import (
        surgically_correct_draft,
    )

    return surgically_correct_draft(
        bodies=bodies,
        reader_verdict=reader_verdict,
        resolved=resolved,
        editor=editor,
    )


__all__ = [
    "SurfaceRenderError",
    "make_surface_node",
    "render_fi_verdict_text",
    "build_fi_margin_surfaces",
    "SurfaceConcept",
    "register_surface_concepts",
    "extract_surface_values",
    "recheck_coherence",
    "PropagationResult",
    "propagate_and_recheck",
    "reconcile_prose_surface",
]
