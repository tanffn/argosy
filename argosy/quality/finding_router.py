"""Phase 3 spine — route a whole-artifact reader finding to its OWNER.

Spec principle 4: "Compliance is an orthogonal gate that routes, never rewrites."
A reader BLOCK is a set of findings; each finding is OWNED by exactly one role.
This module is the deterministic routing decision: subject_type -> owner (+ the
target figure node when the subject is a single canonical figure). Owner truth has
ONE source — for a figure subject it is figure_registry.owner_for(node); only
prose/policy subjects (not a single figure) carry an explicit fallback owner.

Pure: no LLM, no DB, no graph mutation. Converting a RoutedFinding to a
ChangeRequest(OBJECTION) and dispatching the owner through the negotiation ladder
is the Phase-3b integration follow-on. See
docs/superpowers/specs/2026-06-19-financial-advisory-team-design.md (Phase 3).
"""
from __future__ import annotations

from dataclasses import dataclass

from argosy.quality.change_adjudication import (
    Author, AuthorKind, ChangeKind, ChangeRequest,
)
from argosy.quality.figure_registry import OwnerRole, owner_for

# subject_type -> the canonical figure node key. This is BOTH the owner key
# (owner_for resolves it to the accountable role) AND the node key actually
# present in the incremental graph (the CR target for Phase-3b adjudication), so
# the two can't drift. Every key here is in figure_registry.OWNER_MAP (explicit or
# by prefix) and is the key incremental_plan.build_base_graph seeds.
#
# Coverage spans BOTH subject taxonomies (asserted by a test): the reader's
# coherence SUBJECT_REGISTRY AND live_surfaces.CANONICAL_SUBJECT_NODE. The
# canonical-figure subjects (net_worth_*, fi_crossing, retention_*) are not in the
# reader's current prompt taxonomy; routing them is forward-coverage for a finding
# that targets those figures directly (Phase-3b) — never fabricated.
_SUBJECT_RESOLVER_NODE: dict[str, str] = {
    "fi_capital_sufficiency": "retirement.fi_margin_signed_nis",
    "retirement_age_headline": "retirement.earliest_safe_age",
    "fi_crossing": "retirement.fi_crossing_year",
    "net_worth_liquid": "portfolio.liquid_net_worth_nis",
    "net_worth_investable": "portfolio.net_worth_nis",
    "net_worth_total": "portfolio.total_net_worth_incl_residence_nis",
    # The graph node key (US_SITUS_KEY); owner_for maps it to ESTATE (explicit in
    # OWNER_MAP), so it serves as both owner key and graph CR target.
    "us_situs_estate": "concentration.us_situs_estate_nis",
    "retention_at_vest": "tax.retention_at_vest_pct",
    "retention_capital_track": "tax.retention_capital_track_pct",
}

# Subjects that are NOT a single canonical figure — prose/policy contradictions
# (instrument membership, execution-gate, vest policy). They route to an owner but
# have no single target node; the owner fixes the prose/policy, not one figure.
SUBJECT_OWNER_FALLBACK: dict[str, OwnerRole] = {
    "rsu_vest_policy": OwnerRole.EQUITY_COMP,
    "tranche_execution_gate": OwnerRole.INVESTMENT,
    "sgln_ucits_membership": OwnerRole.INVESTMENT,
}

_READER_ROLE = "whole_artifact_reader"


def subject_target_node(subject_type: str) -> str | None:
    """The resolver figure key a subject maps to, or None for prose/policy
    subjects (no single figure)."""
    return _SUBJECT_RESOLVER_NODE.get(subject_type)


def subject_owner(subject_type: str) -> OwnerRole | None:
    """The owner role accountable for a subject. Figure subjects inherit the owner
    from figure_registry.owner_for (one source of truth); prose/policy subjects use
    the explicit fallback. Unknown subject -> None (unrouted, never a crash)."""
    node = _SUBJECT_RESOLVER_NODE.get(subject_type)
    if node is not None:
        return owner_for(node).owner
    return SUBJECT_OWNER_FALLBACK.get(subject_type)


@dataclass(frozen=True)
class RoutedFinding:
    """A reader finding routed to its accountable owner."""

    subject_type: str
    owner: OwnerRole
    target_node_key: str | None
    severity: str
    kind: str
    detail: str
    surfaces_cited: tuple[str, ...] = ()


def route_finding(finding) -> RoutedFinding | None:
    """Route one CoherenceFinding to its owner, or None when its subject_type is
    empty / unknown (unroutable — the caller surfaces it, never silently drops)."""
    subject = getattr(finding, "subject_type", "") or ""
    owner = subject_owner(subject)
    if owner is None:
        return None
    return RoutedFinding(
        subject_type=subject,
        owner=owner,
        target_node_key=subject_target_node(subject),
        severity=getattr(finding, "severity", ""),
        kind=getattr(finding, "kind", ""),
        detail=getattr(finding, "detail", ""),
        surfaces_cited=tuple(getattr(finding, "surfaces_cited", ()) or ()),
    )


def route_verdict(
    verdict, *, severities: tuple[str, ...] = ("BLOCKER", "AMBER", "YELLOW"),
):
    """Split a verdict's findings into (routed, unroutable). Only findings whose
    severity is in ``severities`` are considered; an in-scope finding with no
    routable subject is returned in ``unroutable`` (fail-loud, never dropped)."""
    routed: list[RoutedFinding] = []
    unroutable: list = []
    for f in getattr(verdict, "findings", []) or []:
        if getattr(f, "severity", "") not in severities:
            continue
        r = route_finding(f)
        if r is None:
            unroutable.append(f)
        else:
            routed.append(r)
    return routed, unroutable


def to_change_request(routed: "RoutedFinding | None") -> ChangeRequest | None:
    """Convert a routed FIGURE finding into a ChangeRequest(OBJECTION) against its
    target node — the artifact the Phase-3b cycle integration dispatches to the
    owner via the negotiation ladder. Returns None for a prose/policy subject (no
    single target node) — those route to the owner but aren't a one-node
    objection."""
    if routed is None or routed.target_node_key is None:
        return None
    return ChangeRequest(
        target_node_key=routed.target_node_key,
        author=Author(kind=AuthorKind.AGENT, role=_READER_ROLE),
        kind=ChangeKind.OBJECTION,
        payload={
            # Carry the computed owner so a Phase-3b dispatcher that only has the
            # ChangeRequest still knows who to route to (don't lose owner truth).
            "owner_role": routed.owner.value,
            "severity": routed.severity,
            "finding_kind": routed.kind,
            "surfaces_cited": list(routed.surfaces_cited),
        },
        rationale=routed.detail,
    )


def findings_to_change_requests(
    verdict, *, severities: tuple[str, ...] = ("BLOCKER",),
):
    """Turn a reader verdict into the inputs the remediation loop needs, split
    THREE ways by how each finding is fixed:

      * ``figure_crs``    — ChangeRequest(OBJECTION) for findings that target a
                            single canonical figure node; fed to
                            ``run_incremental_cycle(change_requests=...)`` so the
                            cycle routes each to its owner via the ladder and
                            recomputes only the blast radius (never regenerates).
      * ``prose_routed``  — RoutedFindings that have an owner but NO single figure
                            node (instrument membership, execution-gate, vest
                            policy); the owner fixes the PROSE via the surgical
                            reconcile editor, not a one-node graph edit.
      * ``unroutable``    — findings with no routable subject (empty/unknown
                            subject_type); surfaced, never silently dropped.

    Only findings whose severity is in ``severities`` are considered (default:
    BLOCKER — the promotion-blocking set)."""
    routed, unroutable = route_verdict(verdict, severities=severities)
    figure_crs: list[ChangeRequest] = []
    prose_routed: list[RoutedFinding] = []
    for r in routed:
        cr = to_change_request(r)
        if cr is not None:
            figure_crs.append(cr)
        else:
            prose_routed.append(r)
    return figure_crs, prose_routed, unroutable


__all__ = [
    "SUBJECT_OWNER_FALLBACK",
    "RoutedFinding",
    "subject_target_node",
    "subject_owner",
    "route_finding",
    "route_verdict",
    "to_change_request",
    "findings_to_change_requests",
]
