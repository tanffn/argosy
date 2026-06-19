"""PlanNodeOwnerAgent — the node OWNER's verdict on a proposed change.

The negotiation ladder's peer round (B) is "the owner of the node A wants to
change." The reused ``AnalystResponderAgent`` was wrong for this: its
CONCEDE/REBUT stance is framed relative to an FM *objection*, so it INVERTS in
the "owner responds to a proposed change" frame (the live SWR run had the analyst
say CONCEDE while rejecting the change). This purpose-built agent decides
explicitly RELATIVE TO THE CHANGE:

  * ACCEPT_CHANGE  — the proposed value is right; apply it.
  * REJECT_CHANGE  — the CURRENT value is right; defend it (the arbiter rules
                     direction if A doesn't withdraw).
  * UNRESOLVED     — genuinely unsettled; escalate to the arbiter.

It is grounded in the node's derivation (recipe + inbound values), not in agent-
report citations — so ``require_citations`` is False (the ladder has no prior-
report lineage; the node itself is the evidence). Opus, per accuracy-over-cost.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class PlanNodeOwnerVerdict(BaseModel):
    """The owner's decision on one proposed change to a node it owns."""

    model_config = ConfigDict(extra="ignore")

    stance: Literal["ACCEPT_CHANGE", "REJECT_CHANGE", "UNRESOLVED"] = Field(
        description=(
            "ACCEPT_CHANGE = the proposed value is correct; apply it. "
            "REJECT_CHANGE = the current value is correct; defend it. "
            "UNRESOLVED = genuinely unsettled (needs evidence the arbiter weighs)."
        ),
    )
    reasoning_md: str = Field(
        description=(
            "150-300 words. Reason FROM the node's derivation (recipe + inbound "
            "values) and the change's rationale. State which side the evidence "
            "favors and why. Quote the current and proposed values verbatim."
        ),
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


class PlanNodeOwnerAgent(BaseAgent[PlanNodeOwnerVerdict]):
    """The owner's ACCEPT/REJECT/UNRESOLVED verdict on a proposed node change."""

    agent_role = "plan_node_owner"
    output_model = PlanNodeOwnerVerdict
    require_citations = False

    def build_prompt(
        self,
        *,
        node_key: str,
        current_value: str,
        derivation_md: str,
        proposed_value: str,
        rationale: str,
        prior_turns_md: str = "",
    ) -> tuple[str, str]:
        system = (
            "You are the OWNER of one node in Ariel's living financial plan — the "
            f"derived value `{node_key}`. Another agent proposes to CHANGE it. Your "
            "job is to decide, RELATIVE TO THE PROPOSED CHANGE, whether to "
            "ACCEPT_CHANGE (the proposal is right), REJECT_CHANGE (the current "
            "value is right and you defend it), or mark it UNRESOLVED (genuinely "
            "unsettled — the arbiter will weigh it).\n\n"
            "PRIME DIRECTIVE (above all): Argosy exists to maximize the family's "
            "financial position and secure the earliest SAFE retirement. Do NOT "
            "REJECT_CHANGE out of reflexive conservatism — a change that improves "
            "FI progress within genuine safety bounds should be ACCEPTED. Equally, "
            "do NOT ACCEPT a change that is parameter-fitting to a desired verdict "
            "(e.g. moving an assumption to the edge of its defensible band purely "
            "to flip a conclusion) — that is anti-goal. Decide on the merits of the "
            "derivation, not on which answer is nicer.\n\n"
            "Ground every judgment in the node's DERIVATION (its recipe + inbound "
            "values) below — not in outside memory."
        )
        user_parts = [
            f"NODE: {node_key}",
            f"CURRENT VALUE: {current_value}",
            "",
            "DERIVATION (recipe + inbound values):",
            derivation_md or "(no derivation supplied)",
            "",
            f"PROPOSED NEW VALUE: {proposed_value}",
            f"RATIONALE FOR THE CHANGE: {rationale}",
        ]
        if prior_turns_md.strip():
            user_parts += ["", "PRIOR NEGOTIATION TURNS:", prior_turns_md.strip()]
        user_parts += [
            "",
            "Return ACCEPT_CHANGE / REJECT_CHANGE / UNRESOLVED with reasoning that "
            "cites the current and proposed values verbatim.",
        ]
        return system, "\n".join(user_parts)


__all__ = ["PlanNodeOwnerAgent", "PlanNodeOwnerVerdict"]
