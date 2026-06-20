"""Layer 2 — the author-agnostic change / adjudication substrate.

A ChangeRequest is the single primitive any author (user OR agent_role)
writes against a derivation-graph node (argosy/quality/derivation_graph.py).
Adjudication is ownership- and authority-specific and fail-closed:

  * a DerivedValue target is REJECTED ("change the inputs or the recipe");
  * an InputFact / policy change that flips a HARD verdict is itself an
    adjudicated, audited, contestable change-request (anti-laundering);
  * a recipe / policy change routes through the negotiation ladder.

Pure logic — no DB, no LLM, no I/O. See docs/superpowers/specs/
2026-06-18-living-plan-derivation-graph-design.md (Layer 2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from argosy.quality.derivation_graph import DerivationGraph, NodeKind


class AuthorKind(str, Enum):
    USER = "user"
    AGENT = "agent"


class ChangeKind(str, Enum):
    SET_INPUT = "set_input"        # propose a new value for an InputFact
    SET_RECIPE = "set_recipe"      # propose a new recipe/policy on a recipe node
    SET_DERIVED = "set_derived"    # propose to hand-set a DerivedValue (always rejected)
    OBJECTION = "objection"        # a reader/FM/codex finding against a node


@dataclass(frozen=True)
class Author:
    kind: AuthorKind
    role: str  # "user", "fund_manager", "plan_critique", "codex", ...


@dataclass
class ChangeRequest:
    target_node_key: str
    author: Author
    kind: ChangeKind
    payload: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


class NodeClass(str, Enum):
    INPUT = "input"        # an InputFact — adjudicated, editable by its owner
    DERIVED = "derived"    # never hand-editable; change inputs or recipe
    RECIPE = "recipe"      # a recipe/policy node — routes through the ladder
    SURFACE = "surface"    # a rendered consumer


class OwnershipMap:
    """Who owns each node + node classification. The owner is the authority
    that may accept (or reject) a change against the node. INPUT nodes default
    to the user; everything else can be overridden via ``owners``.

    ``hard_node_keys`` marks nodes whose value is math/derivation/coherence and
    can NEVER be "agreed away" — only an (adjudicated) input change or a recipe
    fix moves them. ``recipe_node_keys`` marks nodes whose VALUE is itself a
    policy/recipe (SWR, NVDA cap, a phase assumption) routed through the ladder.
    """

    def __init__(
        self,
        graph: DerivationGraph,
        *,
        owners: dict[str, str] | None = None,
        hard_node_keys: set[str] | None = None,
        recipe_node_keys: set[str] | None = None,
    ) -> None:
        self._graph = graph
        self._owners = dict(owners or {})
        self._hard = set(hard_node_keys or ())
        self._recipe = set(recipe_node_keys or ())

    def owner_of(self, key: str) -> str:
        if key in self._owners:
            return self._owners[key]
        node = self._graph.get(key)
        # InputFacts default to the user/ingest; derived/surface have no
        # human owner (their owner is "the recipe") — but a caller asking for
        # an owner of a derived node still gets a deterministic answer.
        if node.kind is NodeKind.INPUT:
            return "user"
        return "system"

    def classify(self, key: str) -> NodeClass:
        node = self._graph.get(key)
        if key in self._recipe:
            return NodeClass.RECIPE
        if node.kind is NodeKind.INPUT:
            return NodeClass.INPUT
        if node.kind is NodeKind.SURFACE:
            return NodeClass.SURFACE
        return NodeClass.DERIVED

    def is_hard(self, key: str) -> bool:
        return key in self._hard


class Disposition(str, Enum):
    ACCEPTED = "accepted"            # apply directly (input change, owner-clear)
    REJECTED = "rejected"            # fail-closed; cannot proceed
    NEEDS_LADDER = "needs_ladder"    # route to the negotiation ladder
    NEEDS_AUDIT = "needs_audit"      # verdict-flipping input change — audited + contestable


@dataclass
class AdjudicationOutcome:
    disposition: Disposition
    reason: str = ""


def adjudicate(
    cr: ChangeRequest,
    owners: OwnershipMap,
    *,
    flips_hard_verdict: bool = False,
) -> AdjudicationOutcome:
    """Route a ChangeRequest by ownership + node class, fail-closed.

    Order of checks:
      1. DerivedValue / SET_DERIVED  -> REJECTED (derive-don't-inherit).
      2. Recipe/policy node          -> NEEDS_LADDER (negotiated, not commanded).
      3. Input change that flips a   -> NEEDS_AUDIT (anti-laundering: on the
         hard verdict downstream         record + contestable by owner/arbiter).
      4. Plain input change          -> ACCEPTED.

    ``flips_hard_verdict`` is supplied by the caller (the propagation layer
    knows the blast radius); when an InputFact edit's transitive dependents
    include a hard node whose verdict would change sign, the premise edit is
    not silently accepted.
    """
    # An OBJECTION is a reader/FM/codex FINDING against a node — not an edit. It
    # ROUTES to the node's owner for review/remediation via the ladder; it is
    # never applied directly and never auto-rejected by node class (the owner
    # decides whether to fix an input or the recipe). Checked first so a finding
    # against a DerivedValue still reaches its owner instead of REJECTED.
    if cr.kind is ChangeKind.OBJECTION:
        return AdjudicationOutcome(
            Disposition.NEEDS_LADDER,
            "objection routes to the node owner for review, not a direct edit",
        )
    node_class = owners.classify(cr.target_node_key)
    if node_class is NodeClass.DERIVED or cr.kind is ChangeKind.SET_DERIVED:
        return AdjudicationOutcome(
            Disposition.REJECTED,
            "a DerivedValue is not directly editable — change the inputs or the recipe",
        )
    if node_class is NodeClass.RECIPE:
        return AdjudicationOutcome(
            Disposition.NEEDS_LADDER,
            "recipe/policy change is negotiated through the ladder, not commanded",
        )
    if node_class is NodeClass.INPUT and flips_hard_verdict:
        return AdjudicationOutcome(
            Disposition.NEEDS_AUDIT,
            "premise edit flips a hard verdict — audited + contestable by the owner/arbiter",
        )
    return AdjudicationOutcome(Disposition.ACCEPTED)


class HardNodeError(Exception):
    """A hard (math/derivation/coherence) node cannot be 'agreed away' — the
    only resolutions are fixing an input (adjudicated) or fixing the recipe."""


# Resolution kinds the ladder may apply to a node.
_VALUE_CONCESSION_KINDS = {"concede_value", "agree_value"}
_STRUCTURAL_FIX_KINDS = {"fix_input", "fix_recipe", "rederive"}


def assert_resolvable(
    *, target_node_key: str, owners: OwnershipMap, resolution_kind: str,
) -> None:
    """Guard before a ladder applies a resolution. Raises HardNodeError if a
    HARD node is being resolved by simply conceding/agreeing a value rather
    than by a structural fix (input or recipe)."""
    if owners.is_hard(target_node_key) and resolution_kind in _VALUE_CONCESSION_KINDS:
        raise HardNodeError(
            f"{target_node_key} is a hard node — resolve by fixing an input or "
            f"the recipe, not by conceding the value ({resolution_kind!r})"
        )


__all__ = [
    "AuthorKind", "ChangeKind", "Author", "ChangeRequest",
    "NodeClass", "OwnershipMap",
    "Disposition", "AdjudicationOutcome", "adjudicate",
    "HardNodeError", "assert_resolvable",
]
