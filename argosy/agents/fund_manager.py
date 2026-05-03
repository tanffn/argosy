"""Fund manager agent (SDD §3.5, Appendix B.5, Phase 3).

Final integrity check. Reads the trader proposal, the risk outcome, the
latest plan critique, the user constraints, and the tier; emits
`green_light` or `block`. Default Opus.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class FundManagerDecision(BaseModel):
    """Fund manager's final verdict."""

    decision: Literal["green_light", "block"]
    reason: str = Field(
        description="Specific, cited reason for the decision. For block, "
        "must reference the failing rule (constraint, plan-RED finding, "
        "risk-team REJECT, etc.)."
    )
    required_conditions: list[str] = Field(
        default_factory=list,
        description="Conditions that must hold before execution (empty if "
        "the green-light is unconditional).",
    )
    post_execution_checks: list[str] = Field(
        default_factory=list,
        description="Things the engine should verify after the fill (e.g., "
        "'concentration < 65% post-fill', 'cash >= reserve floor').",
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Top-level distinct citations supporting the decision; "
        "required for the gate.",
    )


class FundManagerAgent(BaseAgent[FundManagerDecision]):
    """Fund manager. Default Opus."""

    agent_role = "fund_manager"
    output_model = FundManagerDecision
    require_citations = True
    max_tokens = 2048

    def __init__(
        self,
        *,
        user_id: str,
        model: str | None = None,
    ) -> None:
        super().__init__(user_id=user_id, model=model or "claude-opus-4-7")

    def build_prompt(
        self,
        *,
        proposal: dict,
        risk_outcome: dict,
        plan_critique: dict | None,
        user_constraints: str,
        tier: str,
    ) -> tuple[str, str]:
        system = (
            "You are the fund manager on the Argosy fleet. Final integrity "
            "check before execution. You decide GREEN_LIGHT or BLOCK with a "
            "specific cited reason.\n\n"
            "GREEN_LIGHT requires:\n"
            "  - All risk officers APPROVE or APPROVE_WITH_CONDITIONS "
            "(consensus_verdict in {APPROVE, APPROVE_WITH_CONDITIONS}).\n"
            "  - Plan-critique has no RED items touching this proposal's "
            "category.\n"
            "  - No inconsistency with user constraints.\n"
            "  - Confidence >= medium (or HIGH for T3).\n\n"
            "BLOCK requires a specific cited reason: which rule failed and "
            "why.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{FundManagerDecision.model_json_schema()}\n"
        )

        plan_block = (
            "(no plan critique available)" if not plan_critique else f"{plan_critique}"
        )
        user = (
            f"Tier: {tier}\n\n"
            "TRADER PROPOSAL:\n"
            f"{proposal}\n\n"
            "RISK OUTCOME (facilitator consensus):\n"
            f"{risk_outcome}\n\n"
            "PLAN CRITIQUE (latest):\n"
            f"{plan_block}\n\n"
            "USER CONSTRAINTS:\n"
            f"{user_constraints}\n\n"
            "Produce the FundManagerDecision JSON now."
        )
        return system, user


__all__ = ["FundManagerAgent", "FundManagerDecision"]
