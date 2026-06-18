"""Hydrate a DerivationGraph from the CURRENT plan (Phase 1b).

INPUT nodes := the resolver's leaf facts (resolved keys with no in-graph
upstream). DERIVED nodes := every resolver manifest key that depends on other
keys; its recipe wraps the resolver derivation (reusing
rederivation_reviewer.standard_recipes for the known load-bearing ones, an echo
recipe for the rest). SURFACE nodes := sections_json sections, with inbound
edges inferred from each citation's source_locator.

Pure core: `hydrate_graph_from_manifest(resolved, sections)` takes an
already-resolved ResolvedPlanNumbers + parsed Section list and returns a
populated DerivationGraph — no DB, no LLM. A thin `hydrate_current_plan`
wrapper does the read-only DB reads. See docs/superpowers/specs/
2026-06-18-living-plan-derivation-graph-design.md (Layer 4).
"""
from __future__ import annotations

from typing import Any, Callable

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind

# Manifest keys that carry a real re-derivation recipe in
# rederivation_reviewer.standard_recipes(). We map the resolver's CANONICAL
# manifest key -> the standard_recipes() recipe key. These derived nodes
# recompute from inputs blind to the stored value (derive-don't-ratify).
KNOWN_RECIPE_KEYS: dict[str, str] = {
    "concentration.nvda_target_sh": "nvda_target_sh",
    "concentration.nvda_sell_sh": "nvda_sell_sh",
    "retirement.fi_margin_signed_nis": "fi_margin_liquid_nis",
}

# Map manifest key -> {inbound manifest key: standard_recipes() argument name}.
# standard_recipes() recipes read argument names (e.g. inp["liquid_nw_nis"]);
# our inbound dict is keyed by manifest keys, so rename before delegating.
KNOWN_RECIPE_ARGMAP: dict[str, dict[str, str]] = {
    "retirement.fi_margin_signed_nis": {
        "portfolio.liquid_net_worth_nis": "liquid_nw_nis",
        "retirement.fi_total_capital_nis": "fi_total_capital_nis",
    },
    "concentration.nvda_target_sh": {
        "concentration.nvda_current_pct": "nvda_weight",
        "concentration.nvda_cap_pct": "cap",
    },
    "concentration.nvda_sell_sh": {
        "concentration.nvda_current_pct": "nvda_weight",
        "concentration.nvda_cap_pct": "cap",
    },
}

# Declared upstream edges for the manifest's DERIVED keys. The tuple is the
# inbound manifest keys each derived value is computed FROM, mirroring the
# resolver's _apply_* derivations so invalidation is EXACT (a change to an
# upstream input invalidates exactly its dependents). Keys absent here are
# leaf INPUT facts (the resolver computes them from the raw snapshot/agent
# rows, which live below the manifest layer this phase models).
MANIFEST_EDGES: dict[str, tuple[str, ...]] = {
    # _apply_fi_margin: liquid_net_worth_nis - fi_total_capital_nis.
    "retirement.fi_margin_signed_nis": (
        "portfolio.liquid_net_worth_nis",
        "retirement.fi_total_capital_nis",
    ),
    # _apply_nvda_deconcentration: derive_nvda_deconcentration(weight, cap, ...).
    "concentration.nvda_target_sh": (
        "concentration.nvda_current_pct",
        "concentration.nvda_cap_pct",
    ),
    "concentration.nvda_sell_sh": (
        "concentration.nvda_current_pct",
        "concentration.nvda_cap_pct",
    ),
}

MISSING_PREFIX = "MISSING:"


def _kind_for_key(key: str, *, has_upstream: bool) -> NodeKind:
    """A manifest key is DERIVED when it has a known recipe OR any in-graph
    upstream edge; otherwise it is a leaf INPUT fact."""
    if key in KNOWN_RECIPE_KEYS or has_upstream:
        return NodeKind.DERIVED
    return NodeKind.INPUT


def _echo_recipe(key: str, value: Any) -> Callable[[dict[str, Any]], Any]:
    """A recipe that reproduces the resolver's value for a derived key that has
    no pure re-derivation recipe yet. Deterministic: recompute == manifest, so
    the round-trip holds while we incrementally promote keys to real recipes."""
    def _r(_inbound: dict[str, Any], _v: Any = value) -> Any:
        return _v
    return _r


def _known_recipe(manifest_key: str) -> Callable[[dict[str, Any]], Any]:
    """Wrap rederivation_reviewer.standard_recipes() for a key with a real blind
    re-derivation recipe, renaming inbound manifest keys to the recipe's own
    argument names via KNOWN_RECIPE_ARGMAP."""
    from argosy.quality.rederivation_reviewer import standard_recipes
    recipe = standard_recipes()[KNOWN_RECIPE_KEYS[manifest_key]]
    argmap = KNOWN_RECIPE_ARGMAP.get(manifest_key, {})

    def _r(inbound: dict[str, Any]) -> Any:
        renamed = {argmap.get(k, k): v for k, v in inbound.items()}
        return recipe(renamed)

    return _r


def build_manifest_nodes(resolved) -> DerivationGraph:
    """Build INPUT + DERIVED nodes for every key in the resolver manifest.

    INPUT  := a resolved leaf fact (no declared upstream + no known recipe),
              OR any pending key (value=None, fail-closed — no invented value).
    DERIVED:= a key with declared MANIFEST_EDGES or a KNOWN_RECIPE_KEYS recipe;
              recompute reproduces the manifest value (echo) unless a real
              standard_recipes() recipe applies.
    """
    g = DerivationGraph()
    for key, rv in resolved.values.items():
        edges = MANIFEST_EDGES.get(key, ())
        is_resolved = rv.status == "resolved" and rv.value is not None
        kind = _kind_for_key(key, has_upstream=bool(edges)) if is_resolved \
            else NodeKind.INPUT
        if kind is NodeKind.INPUT:
            g.add_node(Node(key=key, kind=NodeKind.INPUT,
                            value=rv.value if is_resolved else None))
            continue
        recipe = (_known_recipe(key) if key in KNOWN_RECIPE_KEYS
                  else _echo_recipe(key, rv.value))
        g.add_node(Node(key=key, kind=NodeKind.DERIVED, value=None,
                        inputs=edges, recipe=recipe,
                        compute_version=f"resolver:{key}"))
    return g


def surface_key(section) -> str:
    """Stable surface node key: surface:<horizon>:<section_id>."""
    return f"surface:{section.horizon}:{section.section_id}"


def _manifest_keys_named_by(locator: str, manifest_keys: set[str]) -> set[str]:
    """Manifest keys named by a citation locator (substring match — the resolver
    writes locators that contain the literal manifest key)."""
    return {k for k in manifest_keys if k in (locator or "")}


def add_surface_nodes(g: DerivationGraph, sections) -> None:
    """Add a SURFACE node per Section, with inbound edges to the manifest keys
    its citations name. A citation naming a manifest key NOT present as a
    resolved node in the graph is a DEFECT: we wire a synthetic MISSING:<key>
    valueless INPUT node so the surface is invalid after recompute (fail-closed,
    not silent validity). Pure-render surface recipe echoes body_md."""
    manifest_keys = set(g.keys())
    for section in sections:
        inputs: list[str] = []
        for cite in section.evidence.source_span:
            loc = cite.source_locator or ""
            named = _manifest_keys_named_by(loc, manifest_keys)
            if named:
                inputs.extend(sorted(named))
                continue
            token = loc.split()[0] if loc.split() else ""
            if "." in token and token not in manifest_keys:
                miss_key = MISSING_PREFIX + token
                if miss_key not in g.keys():
                    g.add_node(Node(key=miss_key, kind=NodeKind.INPUT, value=None))
                inputs.append(miss_key)
        seen: set[str] = set()
        ordered = [i for i in inputs if not (i in seen or seen.add(i))]
        has_missing = any(i.startswith(MISSING_PREFIX) for i in ordered)
        g.add_node(Node(
            key=surface_key(section), kind=NodeKind.SURFACE, value=None,
            inputs=tuple(ordered),
            recipe=None if has_missing else _echo_recipe(
                surface_key(section), section.body_md),
            compute_version=f"surface:{section.section_id}:{section.horizon}",
        ))


def recompute_safe(g: DerivationGraph) -> list[str]:
    """Recompute, but SKIP recipe-less SURFACE nodes (defective imports). A
    skipped surface keeps input_hash=None -> stays invalid (a fail-closed defect
    flag) instead of raising. Returns the keys recomputed."""
    recomputed: list[str] = []
    for key in g._topo_order():
        node = g.get(key)
        if node.kind is NodeKind.INPUT:
            continue
        if node.recipe is None:  # defective surface — leave invalid, do not raise
            continue
        if g.is_valid(key):
            continue
        node.value = node.recipe(g._inbound_values(node))
        node.input_hash = g.hash_of(key)
        recomputed.append(key)
    return recomputed


def defective_surfaces(g: DerivationGraph) -> list[str]:
    """SURFACE node keys that are invalid after a safe recompute — the
    fail-closed flags a publish/promotion gate would block on (a cited-but-
    unresolved manifest key, i.e. a missing edge / defective import)."""
    return sorted(
        k for k in g.keys()
        if g.get(k).kind is NodeKind.SURFACE and not g.is_valid(k)
    )


def hydrate_graph_from_manifest(resolved, sections) -> DerivationGraph:
    """Pure hydration: INPUT/DERIVED nodes from the resolved manifest + SURFACE
    nodes from the parsed sections. No DB, no LLM. Raises CycleError if the
    inferred edges form a cycle (spec: detect at hydration, fail loud)."""
    g = build_manifest_nodes(resolved)
    add_surface_nodes(g, sections)
    g.check_acyclic()
    return g


def hydrate_current_plan(session, *, user_id: str, decision_run_id: int) -> DerivationGraph:
    """Hydrate the graph from the current plan: resolve the manifest +
    read PlanVersion.sections_json read-only (the verify_run.py pattern).

    Reads the latest current-or-draft PlanVersion for the user. Sections that
    fail to parse are skipped (logged) — a malformed legacy row degrades to
    fewer surface nodes, never a crash."""
    import json
    import logging

    from sqlalchemy import select

    from argosy.agents.plan_synthesizer_types import Section
    from argosy.services.plan_numeric_resolver import resolve_plan_numbers
    from argosy.state.models import PlanVersion

    log = logging.getLogger(__name__)

    resolved = resolve_plan_numbers(
        session, user_id=user_id, decision_run_id=decision_run_id,
        include_canonical_ages=True,
    )
    pv = session.execute(
        select(PlanVersion)
        .where(PlanVersion.user_id == user_id)
        .where(PlanVersion.role.in_(("current", "draft")))
        .order_by(PlanVersion.id.desc())
    ).scalars().first()

    sections: list[Section] = []
    if pv is not None and pv.sections_json:
        try:
            raw = json.loads(pv.sections_json)
        except (json.JSONDecodeError, ValueError, TypeError):
            raw = []
        for entry in raw if isinstance(raw, list) else []:
            try:
                sections.append(Section.model_validate(entry))
            except Exception as exc:  # noqa: BLE001 — one bad section is skipped
                log.warning("graph_hydration.section_parse_failed err=%s", exc)
    return hydrate_graph_from_manifest(resolved, sections)


__all__ = [
    "KNOWN_RECIPE_KEYS",
    "KNOWN_RECIPE_ARGMAP",
    "MANIFEST_EDGES",
    "MISSING_PREFIX",
    "build_manifest_nodes",
    "add_surface_nodes",
    "recompute_safe",
    "defective_surfaces",
    "surface_key",
    "hydrate_graph_from_manifest",
    "hydrate_current_plan",
]
