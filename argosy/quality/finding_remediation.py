"""Phase 3b-2 — owner remediation seam: turn a routed OBJECTION into a concrete,
targeted fix the cycle/reconcile can apply.

Phase 3a routes a reader finding to its owner; Phase 3b-1 feeds the objection into
``run_incremental_cycle`` so it reaches the owner via the ladder. But an objection
carries no proposed value — the OWNER must propose the remediation. Crucially, a
finding whose subject IS a canonical figure can still need a PROSE fix (e.g. the
earliest-safe age is correct but the withdrawal narrative drifted), so the owner
chooses among:

  * ``set_value``  — the FIGURE is wrong; propose a new value for the target node.
                     Becomes a SET_INPUT ChangeRequest the cycle applies +
                     recomputes the blast radius (never regenerates).
  * ``prose_fix``  — the figure is right; the PROSE drifted. Hand the span to the
                     surgical-reconcile editor (no figure change).
  * ``decline``    — no change warranted (the finding is wrong / not actionable);
                     recorded with a rationale, not silently dropped.

Pure orchestration: the ``RemediationProposer`` (the LLM owner agent) is injected,
so this module is deterministic + unit-testable. See
docs/superpowers/specs/2026-06-19-financial-advisory-team-design.md (Phase 3).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from argosy.quality.change_adjudication import (
    Author, AuthorKind, ChangeKind, ChangeRequest,
)

_SET_VALUE = "set_value"
_PROSE_FIX = "prose_fix"
_DECLINE = "decline"


@dataclass(frozen=True)
class RemediationProposal:
    """An owner's proposed fix for one objection."""

    kind: str  # set_value | prose_fix | decline
    value: float | None = None       # for set_value
    instruction: str = ""            # for prose_fix (what to change) / decline (why)
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.kind not in (_SET_VALUE, _PROSE_FIX, _DECLINE):
            raise ValueError(f"unknown remediation kind {self.kind!r}")
        if self.kind == _SET_VALUE and self.value is None:
            raise ValueError("set_value remediation requires a value")


class RemediationProposer(Protocol):
    """The owner's remediation seam (one live LLM call in production; a fake in
    tests). Given an objection against a node it owns, propose the fix."""

    def propose(
        self, *, target_node_key: str, current_value, derivation_md: str,
        finding_detail: str, surfaces_cited: tuple[str, ...],
    ) -> RemediationProposal:
        ...


@dataclass
class RemediationResult:
    """The outcome of remediating a batch of objections."""

    value_change_requests: list[ChangeRequest] = field(default_factory=list)
    prose_fixes: list[dict] = field(default_factory=list)
    declines: list[dict] = field(default_factory=list)


def _node_context(graph, key: str) -> tuple[object, str]:
    """(current_value, derivation_md) for the target node, or fail-safe defaults
    when no graph / node is available."""
    try:
        if graph is None or key not in set(graph.keys()):
            return None, "(derivation unavailable)"
        node = graph.get(key)
        inbound = {k: graph.get(k).value for k in node.inputs if k in set(graph.keys())}
        deriv = (
            f"computed via {node.compute_version or node.kind.value} from {inbound}"
            if inbound else f"{node.kind.value} (no inbound edges)"
        )
        return node.value, deriv
    except Exception:  # noqa: BLE001 — context is best-effort, never fatal
        return None, "(derivation unavailable)"


def propose_remediations(
    objection_crs, *, proposer: RemediationProposer, graph,
) -> RemediationResult:
    """Ask the OWNER to propose a remediation for each objection change-request.

    Returns a RemediationResult splitting the proposals:
      * ``value_change_requests`` — SET_INPUT ChangeRequests (figure fixes) to feed
        back into ``run_incremental_cycle`` (it adjudicates + applies + recomputes).
      * ``prose_fixes`` — {target_node_key, instruction, rationale, ...} for the
        surgical-reconcile editor.
      * ``declines`` — {target_node_key, rationale, ...} recorded, not dropped.

    A proposer error on one objection is recorded as a decline (fail-safe — one
    unresponsive owner never aborts the batch)."""
    out = RemediationResult()
    for cr in objection_crs:
        if cr.kind is not ChangeKind.OBJECTION:
            # Not an objection — pass it through unchanged (already concrete).
            out.value_change_requests.append(cr)
            continue
        target = cr.target_node_key
        current, deriv = _node_context(graph, target)
        owner_role = cr.payload.get("owner_role", "")
        try:
            proposal = proposer.propose(
                target_node_key=target,
                current_value=current,
                derivation_md=deriv,
                finding_detail=cr.rationale,
                surfaces_cited=tuple(cr.payload.get("surfaces_cited", ()) or ()),
            )
        except Exception as exc:  # noqa: BLE001 — unresponsive owner -> decline
            out.declines.append({
                "target_node_key": target, "owner_role": owner_role,
                "rationale": f"owner remediation unavailable ({exc})",
            })
            continue

        if proposal.kind == _SET_VALUE:
            out.value_change_requests.append(ChangeRequest(
                target_node_key=target,
                author=Author(kind=AuthorKind.AGENT, role=owner_role or "owner"),
                kind=ChangeKind.SET_INPUT,
                payload={"value": proposal.value, "remediation_of": "whole_artifact_reader"},
                rationale=proposal.rationale or proposal.instruction,
            ))
        elif proposal.kind == _PROSE_FIX:
            out.prose_fixes.append({
                "target_node_key": target, "owner_role": owner_role,
                "instruction": proposal.instruction, "rationale": proposal.rationale,
                "surfaces_cited": list(cr.payload.get("surfaces_cited", ()) or ()),
            })
        else:  # decline
            out.declines.append({
                "target_node_key": target, "owner_role": owner_role,
                "rationale": proposal.rationale or proposal.instruction,
            })
    return out


__all__ = [
    "RemediationProposal",
    "RemediationProposer",
    "RemediationResult",
    "propose_remediations",
]
