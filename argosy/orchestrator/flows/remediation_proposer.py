"""Real-proposer seam — the production LLM wiring for owner remediation (Phase 3b-3).

``finding_remediation.propose_remediations`` orchestrates owner remediation but
delegates the actual judgment to an injected ``RemediationProposer`` (so the
engine stays deterministic + unit-testable). This module wires that seam to the
real ``OwnerRemediationAgent`` (Opus): one live claude.exe call per objection,
grounded in the node's live value + derivation.

Mapping (agent verdict -> RemediationProposal):
  * set_value (+ proposed_value) -> RemediationProposal(set_value, value=...)
  * prose_fix (+ instruction)     -> RemediationProposal(prose_fix, instruction=...)
  * decline                       -> RemediationProposal(decline, instruction=reason)
A set_value with a missing/non-numeric proposed_value fails SAFE to a decline (a
malformed figure proposal must never be applied). NEVER constructed in a test path
(it makes a live LLM call); tests inject a deterministic double.
"""
from __future__ import annotations

import logging

from argosy.quality.finding_remediation import RemediationProposal

log = logging.getLogger(__name__)


class RealRemediationProposer:
    """RemediationProposer backed by the real OwnerRemediationAgent."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    def propose(
        self, *, target_node_key: str, current_value, derivation_md: str,
        finding_detail: str, surfaces_cited: tuple[str, ...],
        owner_role: str = "",
    ) -> RemediationProposal:
        from argosy.agents.owner_remediation import OwnerRemediationAgent

        agent = OwnerRemediationAgent(user_id=self.user_id)
        try:
            report = agent.run_sync(
                node_key=target_node_key,
                owner_role=owner_role,
                current_value=repr(current_value),
                derivation_md=derivation_md,
                finding_detail=finding_detail,
                surfaces_cited="\n".join(surfaces_cited or ()),
                decision_id="",
            )
        except Exception as exc:  # noqa: BLE001 — unresponsive owner -> decline
            log.warning("remediation.propose node=%s err=%s", target_node_key, exc)
            return RemediationProposal(
                kind="decline", instruction=f"owner remediation unavailable ({exc})")
        out = getattr(report, "output", report)
        kind = (getattr(out, "remediation", "decline") or "decline").strip()
        reasoning = getattr(out, "reasoning_md", "") or ""
        instruction = getattr(out, "instruction", "") or ""
        log.info("remediation.propose node=%s kind=%s", target_node_key, kind)
        if kind == "set_value":
            raw = getattr(out, "proposed_value", None)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                log.warning("remediation.propose node=%s set_value w/o numeric value "
                            "-> decline", target_node_key)
                return RemediationProposal(
                    kind="decline",
                    instruction="set_value proposed without a numeric value",
                    rationale=reasoning)
            return RemediationProposal(kind="set_value", value=value, rationale=reasoning)
        if kind == "prose_fix":
            return RemediationProposal(
                kind="prose_fix", instruction=instruction, rationale=reasoning)
        return RemediationProposal(
            kind="decline", instruction=instruction, rationale=reasoning)


__all__ = ["RealRemediationProposer"]
