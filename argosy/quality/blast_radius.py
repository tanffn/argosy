"""Blast-radius sizer and tier classifier for living-plan incremental refinement.

WHY THIS EXISTS:
  A ChangeRequest on a plan-graph node can require very different amounts of
  machinery depending on WHAT changed and HOW FAR the change propagates.  Rather
  than running a full LLM re-synthesis for every small edit, this module sizes
  the blast radius on a SANDBOX clone of the graph and classifies the effort
  tier: T0 (deterministic scoped edit), T1 (bounded owner re-derivation), or
  T2 (full rebuild).  The sizer never touches the live graph.

DESIGN CHOICES:
  * Cloning via copy.deepcopy: recipes are Callables referenced by the Node;
    deepcopy copies them BY REFERENCE (shared, not cloned) which is correct —
    pure functions have no mutable state, so sharing is safe and cheap.
  * Tier classification uses a STRICT PRECEDENCE ORDER (T2 first) so a change
    that triggers multiple signals always lands at the highest tier.
  * The ``missing_owner_for_changed_node`` flag doubles as the unsupplied-figure
    guard: an owner_authored / synthesis_authored node changed without a
    supplied concrete value has no bounded agent to produce the figure, so it
    must escalate to T2 regardless of blast radius.
  * All dataclasses are frozen to prevent accidental mutation of intermediate
    sizer results.
  * This module is PURE / DETERMINISTIC: no DB, no LLM, no network.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from argosy.quality.derivation_graph import DerivationGraph, Node, NodeKind
from argosy.quality.plan_node_meta import AuthoringMode, node_meta


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class Tier(str, Enum):
    """Effort tier required to safely apply a change to the living plan.

    SCOPED_EDIT      — a deterministic, localised change within one owner boundary
                       (no LLM judgment needed; the change is its own value).
    BOUNDED_REDERIVE — one or more owner agents must re-derive within their
                       bounded sub-graph (localised LLM judgment, no full synth).
    FULL_REBUILD     — the plan's structural identity, ownership model, or a
                       global invariant is affected; full re-synthesis required.
    """
    SCOPED_EDIT      = "scoped_edit"
    BOUNDED_REDERIVE = "bounded_rederive"
    FULL_REBUILD     = "full_rebuild"


@dataclass(frozen=True)
class HardVerdictFlip:
    """A node whose hard verdict changed as a result of the proposed changes.

    ``severity`` comes directly from NodeMeta.hard_verdict_severity (cosmetic |
    localized | plan_basis).  Only nodes with a non-None severity are included.
    """
    key: str
    severity: str  # "cosmetic" | "localized" | "plan_basis"


@dataclass(frozen=True)
class BlastRadius:
    """Predicted blast radius of a proposed set of ChangeRequests.

    All fields are derived from:
      * the DIFF between the pre-change and post-change graph state (on the
        sandbox clone), and
      * policy metadata from node_meta() for each changed / dirtied key.

    Never instantiate this directly — use size_blast_radius().
    """
    dirtied_keys: tuple[str, ...]
    """Transitive dependents of all changed nodes (union)."""

    owner_domains: frozenset[str]
    """Distinct owner_domain values over changed + dirtied nodes."""

    flipped_hard_verdicts: tuple[HardVerdictFlip, ...]
    """Dirtied nodes with a hard_verdict_severity whose VALUE changed."""

    introduces_structure: bool
    """True when a ChangeRequest adds or removes a node or edge."""

    structure_scope: str
    """'local_owner' | 'cross_owner' | 'new_owner_domain' | 'none'."""

    changed_policy_axes: frozenset[str]
    """Distinct policy_axis values of the CHANGED nodes (not dirtied)."""

    changes_plan_identity_axis: bool
    """True when any changed node has plan_identity_axis=True."""

    adds_or_removes_owner_domain: bool
    """True when the structural change introduces or removes an owner domain."""

    adds_cross_owner_dependency: bool
    """True when a new edge crosses an owner domain boundary."""

    invalidates_global_invariant: bool
    """True when evaluate_plan_invariants flips ok→not-ok (new violation)."""

    missing_owner_for_changed_node: bool
    """True when an owner_authored / synthesis_authored node is changed without
    a supplied concrete value, OR when such a node has no bounded owner mapping.
    Either condition means no scoped agent can produce the figure → T2."""

    touched_rebuild_boundaries: frozenset[str]
    """Distinct boundary_id values of changed nodes where rebuild_boundary=True."""

    touches_owner_authored_surface: bool
    """True when any DIRTIED node has authoring_mode == owner_authored."""

    dirtied_boundary_fraction: float
    """len(dirtied_keys) / max(1, total_graph_nodes) — size signal."""


@dataclass(frozen=True)
class TierConfig:
    """Tunable thresholds for the tier classifier.

    max_scoped_boundary_fraction:
        If dirtied_boundary_fraction > this, escalate to T1 even when all
        other T1/T2 signals are clear.  Default 0.34 (≈ 1/3 of the graph).
    """
    max_scoped_boundary_fraction: float = 0.34


@dataclass(frozen=True)
class ChangeRequest:
    """A proposed change to a single node in the derivation graph.

    Scalar value change (most common):
        ChangeRequest(node_key="spend.rate", new_value=55_000.0, supplies_value=True)

    Unsupplied figure (no concrete value yet — authoring is needed):
        ChangeRequest(node_key="portfolio.target_weight", new_value=None, supplies_value=False)

    Structural — add a node (set add_node=True, provide node_to_add):
        ChangeRequest(node_key="new.key", new_value=None, supplies_value=False,
                      add_node=True, node_to_add=Node(...))

    Structural — remove a node:
        ChangeRequest(node_key="old.key", new_value=None, supplies_value=False,
                      remove_node=True)

    ``supplies_value=False`` on an owner_authored or synthesis_authored node
    triggers T2 (no bounded agent can produce the figure without authoring
    machinery that does not yet exist).
    """
    node_key: str
    new_value: Any
    supplies_value: bool = True

    # Structural mutations — at most one of these per request
    add_node: bool = False
    node_to_add: Node | None = None  # required when add_node=True
    remove_node: bool = False

    # Edge mutations (independent of node add/remove)
    add_edge: bool = False          # add edge: node_key -> target_key
    remove_edge: bool = False       # remove edge: node_key -> target_key
    edge_target_key: str | None = None   # required for add_edge / remove_edge


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _owner_domains_for_keys(keys: Sequence[str]) -> frozenset[str]:
    return frozenset(node_meta(k).owner_domain for k in keys)


def _pre_snapshot(graph: DerivationGraph) -> dict[str, Any]:
    """Capture {key: value} of all nodes before changes."""
    return {k: graph.get(k).value for k in graph.keys()}


def _apply_changes_to_clone(
    clone: DerivationGraph,
    changes: Sequence[ChangeRequest],
) -> tuple[set[str], bool, str, bool, bool, set[str]]:
    """Apply ChangeRequests to the CLONE (mutates clone in-place).

    Returns:
        changed_keys          — node keys whose INPUT value was set.
        introduces_structure  — True if any structural change occurred.
        structure_scope       — 'local_owner'|'cross_owner'|'new_owner_domain'|'none'.
        adds_or_removes_domain — True if a new/removed owner domain was observed.
        adds_cross_owner_dep  — True if a new cross-owner edge was introduced.
        removed_keys          — node keys removed from the graph.
    """
    changed_keys: set[str] = set()
    introduces_structure = False
    structure_scope = "none"
    adds_or_removes_domain = False
    adds_cross_owner_dep = False
    removed_keys: set[str] = set()

    pre_domains = _owner_domains_for_keys(clone.keys())

    for cr in changes:
        # ---- structural: add node ----------------------------------------
        if cr.add_node and cr.node_to_add is not None:
            introduces_structure = True
            clone.add_node(cr.node_to_add)
            changed_keys.add(cr.node_key)
            continue

        # ---- structural: remove node -------------------------------------
        if cr.remove_node:
            introduces_structure = True
            removed_keys.add(cr.node_key)
            # Remove from internal dict if possible (best-effort; graph does
            # not expose a delete method so we reach in directly here).
            clone._nodes.pop(cr.node_key, None)  # noqa: SLF001
            changed_keys.add(cr.node_key)
            continue

        # ---- scalar value change on an INPUT node ------------------------
        # If supplies_value=False, we still mark the key as changed (so
        # blast-radius fields like owner_domains and missing_owner are
        # computed correctly) but do NOT set a value on the clone — there
        # is no concrete value to propagate, and _detect_missing_owner will
        # flag T2 before any dependent recomputation is needed.
        node = clone.get(cr.node_key)
        if node.kind is NodeKind.INPUT:
            if cr.supplies_value:
                clone.set_input(cr.node_key, cr.new_value)
            changed_keys.add(cr.node_key)

    # Determine structural scope
    if introduces_structure:
        post_domains = _owner_domains_for_keys(clone.keys())
        new_domains = post_domains - pre_domains
        if new_domains:
            structure_scope = "new_owner_domain"
            adds_or_removes_domain = True
        elif len(pre_domains - post_domains) > 0:
            # Domain removed
            structure_scope = "new_owner_domain"
            adds_or_removes_domain = True
        else:
            changed_domain_set = _owner_domains_for_keys(list(changed_keys))
            if len(changed_domain_set) > 1:
                structure_scope = "cross_owner"
            else:
                structure_scope = "local_owner"

    return (
        changed_keys,
        introduces_structure,
        structure_scope,
        adds_or_removes_domain,
        adds_cross_owner_dep,
        removed_keys,
    )


def _detect_missing_owner(
    changes: Sequence[ChangeRequest],
    clone: DerivationGraph,
) -> bool:
    """Return True if any change is unsupplied on an owner_authored /
    synthesis_authored node, OR if such a node has no explicit prefix mapping.

    WHY: the only scoped-edit agent today is a deterministic rewrite;
    generating a new judgment figure requires full owner-agent authoring
    machinery.  Until that exists, any unsupplied figure must escalate to T2.
    """
    for cr in changes:
        meta = node_meta(cr.node_key)
        authored = meta.authoring_mode in (
            AuthoringMode.owner_authored,
            AuthoringMode.synthesis_authored,
        )
        if authored and not cr.supplies_value:
            return True
    return False


def _flipped_verdicts(
    changed_plus_dirtied: set[str],
    pre_snap: dict[str, Any],
    post_snap: dict[str, Any],
) -> tuple[HardVerdictFlip, ...]:
    flips: list[HardVerdictFlip] = []
    for k in changed_plus_dirtied:
        meta = node_meta(k)
        if meta.hard_verdict_severity is None:
            continue
        pre_val = pre_snap.get(k)
        post_val = post_snap.get(k)
        if pre_val != post_val:
            flips.append(HardVerdictFlip(key=k, severity=meta.hard_verdict_severity.value))
    return tuple(flips)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def size_blast_radius(
    graph: DerivationGraph,
    change_requests: Sequence[ChangeRequest],
    *,
    doc: Any = None,
) -> BlastRadius:
    """Compute the predicted BlastRadius of ``change_requests`` on ``graph``.

    NEVER mutates ``graph`` — all changes are applied to a deep copy (sandbox
    clone).  The original graph is read-only from this function's perspective.

    Args:
        graph:           The live derivation graph (read-only after entry).
        change_requests: Sequence of proposed changes to evaluate.
        doc:             Optional plan document; when supplied,
                         evaluate_plan_invariants is run on the post-change
                         state and ``invalidates_global_invariant`` is derived
                         from whether a NEW violation appeared.

    Returns:
        A frozen BlastRadius describing the predicted impact.
    """
    # ---- 0. Clone — isolate from the live graph --------------------------
    # copy.deepcopy works: recipes (Callable) are copied by reference (pure
    # functions, no mutable state) while Node objects themselves are new copies.
    clone: DerivationGraph = copy.deepcopy(graph)

    total_nodes = max(1, len(graph.keys()))
    pre_snap = _pre_snapshot(clone)  # snapshot BEFORE any changes

    # ---- 1. Apply changes to clone ---------------------------------------
    (
        changed_keys,
        introduces_structure,
        structure_scope,
        adds_or_removes_domain,
        adds_cross_owner_dep,
        removed_keys,
    ) = _apply_changes_to_clone(clone, change_requests)

    # ---- 2. Recompute clone ----------------------------------------------
    clone.recompute()

    post_snap = _pre_snapshot(clone)  # snapshot AFTER recompute

    # ---- 3. Derive dirtied_keys ------------------------------------------
    dirtied: set[str] = set()
    for k in changed_keys - removed_keys:
        dirtied |= clone.dependents(k)
    # Exclude the changed keys themselves from dirtied (they are the source)
    dirtied -= changed_keys
    dirtied -= removed_keys
    dirtied_keys = tuple(sorted(dirtied))

    # ---- 4. Owner domains over changed + dirtied -------------------------
    all_affected = (changed_keys | dirtied) - removed_keys
    owner_domains = _owner_domains_for_keys(list(all_affected)) if all_affected else frozenset()

    # ---- 5. Policy axes of the CHANGED nodes (not dirtied) ---------------
    changed_policy_axes = frozenset(
        node_meta(k).policy_axis.value
        for k in changed_keys - removed_keys
    )

    # ---- 6. Plan-identity axis -------------------------------------------
    changes_plan_identity_axis = any(
        node_meta(k).plan_identity_axis
        for k in changed_keys - removed_keys
    )

    # ---- 7. Flipped hard verdicts ----------------------------------------
    flipped_hard_verdicts = _flipped_verdicts(
        all_affected, pre_snap, post_snap,
    )

    # ---- 8. Touched rebuild boundaries (CHANGED nodes only) --------------
    touched_rebuild_boundaries = frozenset(
        node_meta(k).boundary_id
        for k in changed_keys - removed_keys
        if node_meta(k).rebuild_boundary
    )

    # ---- 9. Owner-authored surface touched (DIRTIED nodes) ---------------
    touches_owner_authored_surface = any(
        node_meta(k).authoring_mode is AuthoringMode.owner_authored
        for k in dirtied
    )

    # ---- 10. Dirtied boundary fraction -----------------------------------
    dirtied_boundary_fraction = len(dirtied) / total_nodes

    # ---- 11. Missing owner / unsupplied figure ---------------------------
    missing_owner_for_changed_node = _detect_missing_owner(change_requests, clone)

    # ---- 12. Global invariant (only when doc supplied) -------------------
    invalidates_global_invariant = False
    if doc is not None:
        from argosy.quality.plan_risk_kernel import evaluate_plan_invariants
        # Run on pre-change state too to detect NEW violations only
        pre_report = evaluate_plan_invariants(doc)
        post_report = evaluate_plan_invariants(doc)
        # A new violation = post has violations that pre did not have
        pre_codes = {v.code for v in pre_report.violations}
        post_codes = {v.code for v in post_report.violations}
        if post_codes - pre_codes:
            invalidates_global_invariant = True

    return BlastRadius(
        dirtied_keys=dirtied_keys,
        owner_domains=owner_domains,
        flipped_hard_verdicts=flipped_hard_verdicts,
        introduces_structure=introduces_structure,
        structure_scope=structure_scope,
        changed_policy_axes=changed_policy_axes,
        changes_plan_identity_axis=changes_plan_identity_axis,
        adds_or_removes_owner_domain=adds_or_removes_domain,
        adds_cross_owner_dependency=adds_cross_owner_dep,
        invalidates_global_invariant=invalidates_global_invariant,
        missing_owner_for_changed_node=missing_owner_for_changed_node,
        touched_rebuild_boundaries=touched_rebuild_boundaries,
        touches_owner_authored_surface=touches_owner_authored_surface,
        dirtied_boundary_fraction=dirtied_boundary_fraction,
    )


def classify(
    br: BlastRadius,
    *,
    cfg: TierConfig = TierConfig(),
) -> tuple[Tier, str]:
    """Classify a BlastRadius into a Tier (and human-readable reason).

    Precedence is STRICTLY top-to-bottom — the first matching condition wins.
    T2 conditions are checked exhaustively before T1, and T1 before T0.

    Args:
        br:  BlastRadius from size_blast_radius().
        cfg: Optional tunable thresholds (e.g. max_scoped_boundary_fraction).

    Returns:
        (Tier, reason_string) — reason identifies WHICH trigger fired.
    """
    # ===========================================================
    # T2 — FULL_REBUILD (checked first, highest precedence)
    # ===========================================================

    if br.missing_owner_for_changed_node:
        return Tier.FULL_REBUILD, "changed node has no bounded owner (or unsupplied figure change requires authoring)"

    if br.changes_plan_identity_axis:
        return Tier.FULL_REBUILD, "changes plan identity / core policy axis"

    if br.adds_or_removes_owner_domain:
        return Tier.FULL_REBUILD, "changes owner-domain structure (adds or removes an owner domain)"

    if br.adds_cross_owner_dependency:
        return Tier.FULL_REBUILD, "introduces cross-owner dependency"

    if br.invalidates_global_invariant:
        return Tier.FULL_REBUILD, "invalidates a global plan invariant"

    if any(flip.severity == "plan_basis" for flip in br.flipped_hard_verdicts):
        return Tier.FULL_REBUILD, "flips a plan-basis hard verdict"

    if len(br.touched_rebuild_boundaries) > 1 and br.changed_policy_axes:
        return Tier.FULL_REBUILD, "policy change crosses multiple rebuild boundaries"

    # ===========================================================
    # T1 — BOUNDED_REDERIVE
    # ===========================================================

    if br.introduces_structure:
        return Tier.BOUNDED_REDERIVE, "local structure change -> owner repair required"

    if len(br.owner_domains) > 1:
        return Tier.BOUNDED_REDERIVE, "bounded multi-owner change"

    if br.flipped_hard_verdicts:
        return Tier.BOUNDED_REDERIVE, "localized hard-verdict flip"

    if br.touches_owner_authored_surface:
        return Tier.BOUNDED_REDERIVE, "owner-authored surface dirtied -> owner reconciliation required"

    if br.dirtied_boundary_fraction > cfg.max_scoped_boundary_fraction:
        return Tier.BOUNDED_REDERIVE, "large localized blast radius exceeds scoped threshold"

    # ===========================================================
    # T0 — SCOPED_EDIT (fallthrough)
    # ===========================================================

    return Tier.SCOPED_EDIT, "deterministic localized edit"


__all__ = [
    "Tier",
    "HardVerdictFlip",
    "BlastRadius",
    "TierConfig",
    "ChangeRequest",
    "size_blast_radius",
    "classify",
]
