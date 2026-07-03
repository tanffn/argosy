"""Per-node policy metadata for the living-plan derivation graph.

WHY THIS EXISTS:
  The smart-refinement classifier needs to decide, for any changed node, whether
  the impact is a scoped edit, a bounded owner re-derivation, or a full rebuild.
  That decision requires policy metadata anchored on the DOTTED NODE KEY —
  the same key used by DerivationGraph.  This module is the authoritative source
  for that metadata.

  It deliberately does NOT own the derivation graph itself (derivation_graph.py),
  the change/adjudication logic (change_adjudication.py), or the LLM-agent wiring
  (ladder_participants.py).  It only resolves a key → NodeMeta via a pure,
  deterministic, prefix-policy table.

DESIGN CHOICES:
  * _OWNER_BY_PREFIX and _DEFAULT_OWNER_ROLE are DELIBERATELY DUPLICATED here
    (not imported from ladder_participants) so this module stays import-pure
    (ladder_participants has side-effectful agent imports).  The duplicates MUST
    be kept in sync with ladder_participants; the drift-guard test in
    tests/test_plan_node_meta.py will fail if they diverge.
  * _LOCAL_OWNER_EXTENSIONS maps additional prefix→owner domains that are
    known to this module but not yet wired in ladder_participants (e.g. the
    "allocation" topic-owner agent, which is not yet built).  These are NOT
    subject to the drift guard.
  * Unknown keys fall through to a CONSERVATIVE DEFAULT (synthesis_authored,
    default owner) — fail-closed so validate_owner_coverage can surface gaps.
  * plan_identity_axis keys are EXPLICIT and small — only keys whose change
    would invalidate the plan's core identity (risk posture, objective, residency).
    These are NOT derived from the prefix table; they are named individually.
    plan_identity_axis=True is an OVERRIDE: the downstream tier classifier MUST
    check this flag FIRST (before authoring_mode) — a plan-identity change always
    escalates to full rebuild regardless of authoring_mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PolicyAxis(str, Enum):
    """Which policy dimension a node primarily touches.

    A node belongs to exactly one axis; the axis determines which owner agent
    is the primary reviewer and what blast-radius boundary applies.
    """
    risk          = "risk"
    withdrawal    = "withdrawal"
    tax           = "tax"
    allocation    = "allocation"
    estate        = "estate"
    concentration = "concentration"
    execution     = "execution"
    prose         = "prose"


class AuthoringMode(str, Enum):
    """How the node value is produced.

    deterministic    — a pure function of inputs (no LLM, no human judgment).
    owner_authored   — the bounded owner agent produces the value via LLM judgment.
    synthesis_authored — the synthesis LLM produces it (cross-cutting, no bounded owner).
    """
    deterministic      = "deterministic"
    owner_authored     = "owner_authored"
    synthesis_authored = "synthesis_authored"


class HardVerdictSeverity(str, Enum):
    """How severe a hard verdict on this node is when it fires.

    cosmetic   — display / formatting issue; no rebuild needed.
    localized  — fix is scoped within the owner boundary.
    plan_basis — the plan's investment basis is affected; may trigger full rebuild.
    """
    cosmetic    = "cosmetic"
    localized   = "localized"
    plan_basis  = "plan_basis"


# ---------------------------------------------------------------------------
# NodeMeta dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeMeta:
    """Policy metadata for a single derivation-graph node.

    Fields:
      owner_domain          — the analyst/agent role that owns this node (matches
                              _OWNER_BY_PREFIX convention in ladder_participants).
      policy_axis           — which policy axis this node primarily belongs to.
      authoring_mode        — how the node value is produced (deterministic /
                              owner_authored / synthesis_authored).
      boundary_id           — the rebuild-boundary this node lives in; changes
                              that stay within a boundary can avoid full rebuild.
      rebuild_boundary      — True if THIS node is a boundary node (its change
                              triggers a bounded re-derivation, not a full rebuild).
      plan_identity_axis    — True for the small set of keys whose change
                              invalidates the plan's core identity (risk posture,
                              objective, tax residency).  Changes to these ALWAYS
                              require a full rebuild + user confirmation.
                              OVERRIDE: the downstream tier classifier MUST check
                              this flag FIRST (before authoring_mode) — a
                              plan-identity change always escalates to full rebuild
                              regardless of authoring_mode.
      hard_verdict_severity — Severity when a hard verdict fires on this node, or
                              None if this node has no hard verdict gate.
    """
    owner_domain:          str
    policy_axis:           PolicyAxis
    authoring_mode:        AuthoringMode
    boundary_id:           str
    rebuild_boundary:      bool
    plan_identity_axis:    bool
    hard_verdict_severity: HardVerdictSeverity | None


# ---------------------------------------------------------------------------
# Ownership anchors — deliberately duplicated from ladder_participants for
# import-purity (ladder_participants has side-effectful agent imports).
# MUST be kept in sync with ladder_participants; the drift-guard test will
# fail on divergence.  Do NOT add new entries here — use _LOCAL_OWNER_EXTENSIONS
# for domains that are not yet wired in ladder_participants.
# ---------------------------------------------------------------------------

_DEFAULT_OWNER_ROLE = "withdrawal_sequencer"

# Sorted longest-prefix-first so the loop is unambiguous (longest match wins).
_OWNER_BY_PREFIX: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            ("retirement.", "withdrawal_sequencer"),
            ("spend.",      "household_budget"),
            ("concentration.", "concentration"),
            ("fx.",         "fx"),
            ("savings.",    "equity_comp"),
        ),
        key=lambda t: len(t[0]),
        reverse=True,
    )
)

# Additional owner domains known to this module but not yet wired in
# ladder_participants (e.g. allocation topic-owner agent not yet built).
# NOT subject to the drift guard.  Sorted longest-prefix-first.
_LOCAL_OWNER_EXTENSIONS: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            # allocation topic-owner agent not yet built (known gap); domain
            # is named here so coverage validation sees these as explicitly mapped.
            ("portfolio.",  "allocation"),
            ("allocation.", "allocation"),
            ("sleeve",      "allocation"),
        ),
        key=lambda t: len(t[0]),
        reverse=True,
    )
)

# Combined lookup: _OWNER_BY_PREFIX first, then extensions. Sorted longest-first.
_ALL_OWNER_PREFIXES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        _OWNER_BY_PREFIX + _LOCAL_OWNER_EXTENSIONS,
        key=lambda t: len(t[0]),
        reverse=True,
    )
)


# ---------------------------------------------------------------------------
# INITIAL POLICY-SEED TABLE
# Initial mapping: prefix → (policy_axis, authoring_mode, boundary_id,
#                             rebuild_boundary, hard_verdict_severity).
# Tune this table as the graph matures.  The keys here MUST be dotted prefixes
# or exact keys; prefix matching is longest-prefix-wins.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PrefixPolicy:
    policy_axis:           PolicyAxis
    authoring_mode:        AuthoringMode
    boundary_id:           str
    rebuild_boundary:      bool
    hard_verdict_severity: HardVerdictSeverity | None


_PREFIX_POLICY: tuple[tuple[str, _PrefixPolicy], ...] = (
    # ---- withdrawal / retirement planning -------------------------------- #
    ("retirement.", _PrefixPolicy(
        policy_axis=PolicyAxis.withdrawal,
        authoring_mode=AuthoringMode.owner_authored,
        boundary_id="withdrawal",
        rebuild_boundary=True,
        hard_verdict_severity=HardVerdictSeverity.plan_basis,
    )),
    ("spend.", _PrefixPolicy(
        policy_axis=PolicyAxis.withdrawal,
        authoring_mode=AuthoringMode.owner_authored,
        boundary_id="withdrawal",
        rebuild_boundary=False,
        hard_verdict_severity=HardVerdictSeverity.localized,
    )),
    # ---- concentration risk --------------------------------------------- #
    ("concentration.", _PrefixPolicy(
        policy_axis=PolicyAxis.concentration,
        authoring_mode=AuthoringMode.deterministic,
        boundary_id="concentration",
        rebuild_boundary=True,
        hard_verdict_severity=HardVerdictSeverity.plan_basis,
    )),
    # ---- allocation / portfolio / sleeve --------------------------------- #
    # allocation topic-owner agent not yet built (known gap); domain named
    # explicitly so the policy table is consistent with _LOCAL_OWNER_EXTENSIONS.
    ("portfolio.", _PrefixPolicy(
        policy_axis=PolicyAxis.allocation,
        authoring_mode=AuthoringMode.owner_authored,
        boundary_id="allocation",
        rebuild_boundary=False,
        hard_verdict_severity=HardVerdictSeverity.localized,
    )),
    ("allocation.", _PrefixPolicy(
        policy_axis=PolicyAxis.allocation,
        authoring_mode=AuthoringMode.owner_authored,
        boundary_id="allocation",
        rebuild_boundary=True,
        hard_verdict_severity=HardVerdictSeverity.plan_basis,
    )),
    # sleeve* catches both "sleeve." and "sleeve_us." etc.
    ("sleeve", _PrefixPolicy(
        policy_axis=PolicyAxis.allocation,
        authoring_mode=AuthoringMode.owner_authored,
        boundary_id="allocation",
        rebuild_boundary=False,
        hard_verdict_severity=HardVerdictSeverity.localized,
    )),
    # ---- FX — tax axis (domicile / §102 calculations) ------------------- #
    ("fx.", _PrefixPolicy(
        policy_axis=PolicyAxis.tax,
        authoring_mode=AuthoringMode.deterministic,
        boundary_id="fx",
        rebuild_boundary=False,
        hard_verdict_severity=HardVerdictSeverity.localized,
    )),
    # ---- savings / equity comp — feeds withdrawal/FI projection ---------- #
    ("savings.", _PrefixPolicy(
        policy_axis=PolicyAxis.withdrawal,
        authoring_mode=AuthoringMode.deterministic,
        boundary_id="savings",
        rebuild_boundary=False,
        hard_verdict_severity=None,
    )),
)

# Longest-prefix-first sorted order for unambiguous matching.
_SORTED_POLICY: tuple[tuple[str, _PrefixPolicy], ...] = tuple(
    sorted(_PREFIX_POLICY, key=lambda t: len(t[0]), reverse=True)
)

# ---- Plan-identity keys (EXPLICIT, small, named individually) ------------ #
# A change to any of these keys invalidates the plan's core identity and
# ALWAYS requires a full rebuild + user confirmation (plan_identity_axis=True).
# Document each: why it is an identity key, not just a parameter.
_PLAN_IDENTITY_KEYS: frozenset[str] = frozenset({
    # risk posture: the plan's risk tolerance is the top-level policy choice;
    # every allocation, SWR, and glide path derives from it.
    "retirement.risk_posture",
    # objective: "earliest safe retirement" vs "maximise legacy" changes the
    # entire optimisation direction and the plan's acceptance criteria.
    "retirement.objective",
    # tax residency: changes the domicile/estate gate, the §102 calculus, and
    # which instruments are permissible (UCITS vs US-situs).
    "retirement.tax_residency",
})

# Conservative default used for any key that matches no prefix.
_DEFAULT_POLICY = _PrefixPolicy(
    policy_axis=PolicyAxis.prose,
    authoring_mode=AuthoringMode.synthesis_authored,
    boundary_id="default",
    rebuild_boundary=False,
    hard_verdict_severity=None,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def node_meta(key: str) -> NodeMeta:
    """Resolve a dotted node key to its NodeMeta policy record.

    Resolution order:
      1. Exact-key match against _PLAN_IDENTITY_KEYS (plan_identity_axis=True).
      2. Longest-prefix match against _SORTED_POLICY.
      3. Conservative default (_DEFAULT_OWNER_ROLE, synthesis_authored).

    Pure and deterministic — no I/O, no LLM, no DB.

    NOTE: plan_identity_axis=True is an OVERRIDE.  The downstream tier
    classifier MUST check this flag FIRST (before authoring_mode) — a
    plan-identity change always escalates to full rebuild regardless of
    authoring_mode.
    """
    is_identity = key in _PLAN_IDENTITY_KEYS

    # Longest-prefix match against policy table
    policy: _PrefixPolicy | None = None
    for prefix, ppol in _SORTED_POLICY:
        if key.startswith(prefix):
            policy = ppol
            break

    # Owner domain — longest-prefix match against combined owner table
    # (_OWNER_BY_PREFIX synced with ladder_participants + _LOCAL_OWNER_EXTENSIONS
    # for domains not yet wired there).  Already sorted longest-first.
    matched_owner: str | None = None
    for prefix, owner in _ALL_OWNER_PREFIXES:
        if key.startswith(prefix):
            matched_owner = owner
            break

    if policy is None:
        policy = _DEFAULT_POLICY
    if matched_owner is None:
        matched_owner = _DEFAULT_OWNER_ROLE

    return NodeMeta(
        owner_domain=matched_owner,
        policy_axis=policy.policy_axis,
        authoring_mode=policy.authoring_mode,
        boundary_id=policy.boundary_id,
        rebuild_boundary=policy.rebuild_boundary,
        plan_identity_axis=is_identity,
        hard_verdict_severity=policy.hard_verdict_severity,
    )


def validate_owner_coverage(graph: Any) -> list[str]:
    """Return keys that are non-INPUT, owner_authored or synthesis_authored,
    and fall through to the default owner mapping.

    Deterministic nodes are owner-agnostic (pure functions) and are NOT
    flagged even if they have no explicit owner prefix — only nodes where
    LLM judgment is involved (owner_authored / synthesis_authored) require
    a bounded owner assignment.

    These are nodes with NO explicit bounded owner — the caller should treat
    this as a fail-loud condition (fail-closed: an unowned mutable node is a
    gap in the policy table, not a silent default).

    Args:
        graph: any object with .keys() -> list[str] and .get(key) -> node with
               node.kind.value == "input" | "derived" | "surface".

    Returns:
        List of node keys that resolve to the conservative default (no explicit
        prefix match in _ALL_OWNER_PREFIXES) AND whose authoring_mode is
        owner_authored or synthesis_authored.  Empty list = full coverage.
    """
    unmapped: list[str] = []
    for key in graph.keys():
        node = graph.get(key)
        # INPUT nodes are authoritative sources — they ARE the boundary; no
        # bounded owner is needed (the user/ingest pipeline IS the owner).
        if getattr(node.kind, "value", node.kind) == "input":
            continue
        # Deterministic nodes are pure functions — owner-agnostic, not flagged.
        meta = node_meta(key)
        if meta.authoring_mode is AuthoringMode.deterministic:
            continue
        # Check if the key falls through the ownership table to the default.
        matched = any(key.startswith(prefix) for prefix, _ in _ALL_OWNER_PREFIXES)
        if not matched:
            unmapped.append(key)
    return unmapped


__all__ = [
    "PolicyAxis",
    "AuthoringMode",
    "HardVerdictSeverity",
    "NodeMeta",
    "node_meta",
    "validate_owner_coverage",
]
