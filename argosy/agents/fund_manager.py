"""Fund manager agent (SDD §3.5, Appendix B.5, Phase 3).

Final integrity check. Reads the trader proposal, the risk outcome, the
latest plan critique, the user constraints, and the tier; emits
`green_light` or `block`. Default Opus.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class FundManagerDecision(BaseModel):
    """Fund manager's final verdict (trade-proposal flow)."""

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


class FundManagerPlanRevisionDecision(BaseModel):
    """Fund manager's plan-revision integrity verdict (plan_synthesis Phase 5).

    Schema is intentionally simpler than ``FundManagerDecision`` because
    this is not a trade approval — it is a plan-level structural check.
    ``cited_sources`` is required so the base-class citation gate passes.
    """

    approved: bool = Field(
        description="True if the synthesized plan honours all hard "
        "constraints and the three horizons cohere."
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="Bullet-point justification for the verdict. Must be "
        "non-empty when approved=False.",
    )
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Citations backing the verdict (domain_kb paths, "
        "risk-verdict references, plan-section refs).",
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
        # Tracks which schema to validate against; set during build_prompt.
        self._current_decision_kind: str = "trade_proposal"

    def build_prompt(
        self,
        *,
        decision_kind: str = "trade_proposal",
        **kw,
    ) -> tuple[str, str]:
        """Dispatch on ``decision_kind``.

        - ``"trade_proposal"`` (default): legacy execution-time integrity
          check used by Phase 3 trade-execution flow.
        - ``"plan_revision"``: plan-revision integrity check used by the
          plan_synthesis flow's Phase 5 (Wave 2). Validates against
          ``FundManagerPlanRevisionDecision`` instead of
          ``FundManagerDecision``.
        """
        # Stash for _parse_output to pick the correct schema.
        self._current_decision_kind = decision_kind
        if decision_kind == "plan_revision":
            return self._build_plan_revision_prompt(**kw)
        return self._build_trade_proposal_prompt(**kw)

    def _parse_output(self, text: str) -> BaseModel:
        """Select the output model based on the current decision_kind."""
        import json

        # Tolerate fenced code blocks.
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        data = json.loads(cleaned)

        if self._current_decision_kind == "plan_revision":
            return FundManagerPlanRevisionDecision.model_validate(data)
        return FundManagerDecision.model_validate(data)

    def _build_trade_proposal_prompt(
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

    def _build_plan_revision_prompt(
        self,
        *,
        draft_plan: str,
        risk_verdict: str,
        user_directive: str = "",
    ) -> tuple[str, str]:
        """Plan-revision integrity check (Wave 2 Phase 5).

        This is NOT a trade approval — it asks the fund manager to verify
        the synthesized plan honours hard constraints, that the three
        horizons cohere, and that every claimed target has rationale and
        cited evidence.

        ``user_directive``: when non-empty, the system prompt includes a
        section telling the FM to respect the user's per-objection stances
        from the prior round (AGREED / DISAGREED / DEFERRED). When empty
        (default), the section is omitted so the prompt is byte-identical
        to the pre-feature behavior. This is how the
        /api/plan/draft/objections/start-new-round payload reaches the FM
        so it stops re-raising objections the user has already resolved.
        """
        from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER

        system = (
            "You are the fund manager on the Argosy fleet. This is a "
            "plan-revision integrity check, not a trade approval.\n\n"
            f"{AUTHORITY_DISCLAIMER}\n\n"
            "Validate: (a) distillate hard-constraints honored; (b) three "
            "horizons cohere; (c) every target has rationale + cited source; "
            "(d) 'no_change' is justified by evidence if claimed.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{FundManagerPlanRevisionDecision.model_json_schema()}\n"
        )
        # User directive — same fix as plan_synthesizer (see commit
        # following f8faaca): keep variable content in the user prompt
        # to avoid the bundled claude.exe SDK's empty-output path
        # observed when large variable content sits in the system
        # prompt. System prompt gets a short pointer instead.
        if user_directive:
            system = system + (
                "\nUSER DIRECTIVE PRESENT: a USER DIRECTIVE block appears in the "
                "user message below capturing the human's per-objection stances "
                "from the prior round. Respect the user's resolved positions:\n"
                "  - For AGREED objections: do NOT re-raise these. The user accepted them.\n"
                "  - For DISAGREED objections: evaluate the synthesizer's response to the user's counter-position\n"
                "    on its merits. If the synthesizer correctly honored the user's counter-position, do not\n"
                "    re-raise the original objection.\n"
                "  - For DEFERRED objections: evaluate freshly.\n"
                "You retain authority to raise NEW objections on issues the user has not addressed, or to call\n"
                "out where the synthesizer has ignored a load-bearing directive.\n"
            )
        directive_section = (
            f"=== USER DIRECTIVE (authoritative human input on this run) ===\n{user_directive}\n\n"
            if user_directive else ""
        )
        user = (
            f"{directive_section}"
            f"=== DRAFT PLAN ===\n{draft_plan}\n\n"
            f"=== CONSOLIDATED RISK VERDICT ===\n{risk_verdict}\n\n"
            "Return your JSON verdict now."
        )
        return system, user


__all__ = ["FundManagerAgent", "FundManagerDecision", "FundManagerPlanRevisionDecision"]
