"""Risk officer agent (SDD §3.4, Appendix B.4, Phase 3).

A single class parameterized by `perspective`: `aggressive`, `neutral`,
or `conservative`. Each perspective has a different stance baked into
the prompt. Default Sonnet.

Three instances debate (one per perspective) for N rounds; the risk
facilitator extracts the consensus.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from argosy.agents._plan_authority import (
    CONSERVATIVE_FI_COUNTERWEIGHT,
    PRIME_DIRECTIVE,
)
from argosy.agents.base import BaseAgent, ConfidenceBand


Perspective = Literal["aggressive", "neutral", "conservative"]


class CitedConcern(BaseModel):
    """One specific risk concern with cited evidence."""

    concern: str = Field(description="One-sentence statement of the concern.")
    evidence: str = Field(
        description="Concrete evidence (a number, a constraint citation, "
        "a domain_knowledge rule)."
    )
    cited_sources: list[str] = Field(default_factory=list)


class RiskVerdict(BaseModel):
    """Output of one risk officer per round."""

    perspective: Perspective
    round_index: int = Field(ge=1)
    verdict: Literal["APPROVE", "APPROVE_WITH_CONDITIONS", "REJECT"]
    conditions: list[str] = Field(
        default_factory=list,
        description="Required only for APPROVE_WITH_CONDITIONS. e.g., "
        "'cut size 50%', 'tighten stop to -3%', 'postpone to next week'.",
    )
    concerns: list[CitedConcern] = Field(
        default_factory=list,
        description="3-5 specific concerns, each cited.",
    )
    response_to_opposing: str = Field(
        default="",
        description="Direct response to the strongest opposing risk-officer "
        "point from the prior round; empty for round 1.",
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Top-level distinct cited sources, required for the gate.",
    )


_PERSPECTIVE_INSTRUCTIONS: dict[str, str] = {
    "aggressive": (
        "AGGRESSIVE perspective: tolerate vol/drawdown if Sharpe-improving. "
        "Flag missed alpha. Push back on excess caution. Approve when the "
        "risk-adjusted upside is clearly positive even if the worst case is "
        "ugly."
    ),
    "neutral": (
        "NEUTRAL perspective: balanced view. Flag inconsistencies between "
        "the proposal and the user's constraints, between the trader's "
        "rationale and the analyst evidence, and between the proposed size "
        "and the configured caps."
    ),
    "conservative": (
        "CONSERVATIVE perspective: capital-preservation-first. Surface the "
        "worst-case path. Reject if drawdown could violate the user's "
        "stated risk caps. Recommend size cuts, tighter stops, or postpone "
        "decisions that lack high-confidence inputs."
    ),
}


class RiskOfficerAgent(BaseAgent[RiskVerdict]):
    """One risk officer; perspective set at construction time."""

    agent_role = "risk_officer"
    output_model = RiskVerdict
    require_citations = True
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (16000).

    def __init__(
        self,
        *,
        user_id: str,
        perspective: Perspective,
        model: str | None = None,
    ) -> None:
        # H6: pass model through (None → the role default in
        # DEFAULT_MODEL_BY_ROLE, i.e. Opus). The prior ``or "claude-sonnet-4-6"``
        # silently shadowed that default, running the RISK GATE on Sonnet against
        # the accuracy-over-cost binding preference.
        super().__init__(user_id=user_id, model=model)
        if perspective not in ("aggressive", "neutral", "conservative"):
            raise ValueError(f"unknown risk perspective: {perspective!r}")
        self.perspective: Perspective = perspective

    def build_prompt(
        self,
        *,
        proposal: dict,
        analyst_reports: list[dict],
        user_constraints: str,
        risk_caps: dict,
        prior_rounds: list[dict] | None = None,
        round_index: int = 1,
        n_max: int = 2,
        user_directive: str = "",
    ) -> tuple[str, str]:
        prior_rounds = prior_rounds or []
        instructions = _PERSPECTIVE_INSTRUCTIONS[self.perspective]
        # Prime directive lands in every risk officer's system prompt so
        # the risk gate weighs concerns against the goal (earliest safe
        # retirement), not risk-avoidance alone. The conservative officer
        # — whose default failure mode is over-caution that quietly delays
        # FI — additionally gets the cost-in-years counterweight.
        directive_block = PRIME_DIRECTIVE
        if self.perspective == "conservative":
            directive_block = f"{PRIME_DIRECTIVE}\n{CONSERVATIVE_FI_COUNTERWEIGHT}"
        system = (
            f"You are the {self.perspective} risk officer on the Argosy fleet. "
            f"Round {round_index} of {n_max}. Other risk officers may have "
            "argued differently in this and prior rounds.\n\n"
            f"{directive_block}\n\n"
            f"{instructions}\n\n"
            "Rules:\n"
            "  - Verdict is one of APPROVE / APPROVE_WITH_CONDITIONS / REJECT.\n"
            "  - For APPROVE_WITH_CONDITIONS, list specific, actionable "
            "conditions (size cut percentage, stop-tightening level, etc.).\n"
            "  - 3-5 concerns, each with concrete evidence and a citation.\n"
            "  - For round > 1, address the strongest opposing point in "
            "`response_to_opposing`.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{RiskVerdict.model_json_schema()}\n"
        )

        # User directive — authoritative input from the human captured on
        # this synthesis run. Same pattern as plan_synthesizer.py /
        # fund_manager.py (post-a5d317c): a short DIRECTIVE POINTER lives
        # in the SYSTEM prompt; the verbatim directive content lives at
        # the TOP of the USER prompt below. Variable content in system
        # prompts has reproducibly triggered the bundled claude.exe SDK's
        # empty-output path (synthesis #27/#28).
        if user_directive:
            system = system + (
                "\nUSER DIRECTIVE PRESENT: a USER DIRECTIVE block appears in the "
                "user message below capturing the human's per-objection stances "
                "from the prior round. Respect the user's resolved positions:\n"
                "  - Where the user has AGREED a risk is acceptable, you may "
                "note residual concern as part of `concerns` but should not "
                "block the proposal on that resolved item alone — don't REJECT "
                "solely on a risk the user has accepted.\n"
                "  - Where the user has DISAGREED with a prior concern and "
                "supplied a counter-position, treat the counter-position as "
                "authoritative on that point.\n"
                "  - DEFERRED items: judge freshly.\n"
                "You retain authority to flag NEW risks the user has not "
                "addressed, especially if they breach configured risk caps.\n"
            )

        prior_block = ""
        if prior_rounds:
            chunks: list[str] = []
            for t in prior_rounds:
                p = t.get("perspective", "?")
                v = t.get("verdict", "?")
                cs = t.get("concerns", [])
                chunks.append(f"  - {p} ({v}): {cs}")
            prior_block = "\n\nPRIOR ROUNDS:\n" + "\n".join(chunks)

        report_blocks: list[str] = []
        for r in analyst_reports:
            role = r.get("agent_role") or r.get("role") or "?"
            payload = {k: v for k, v in r.items() if k not in ("agent_role", "role")}
            report_blocks.append(f"### {role}\n{payload}")

        # User directive lives at the TOP of the user prompt (when
        # present) so the model encounters it before the rest of the
        # context. Empty (default) omits the section entirely.
        directive_prefix = ""
        if user_directive:
            directive_prefix = (
                "=== USER DIRECTIVE (authoritative human input on this run) ===\n"
                + user_directive
                + "\n\n"
            )

        user = (
            f"{directive_prefix}"
            f"Your perspective: {self.perspective}\n"
            f"Round {round_index} of {n_max}.\n\n"
            "TRADER PROPOSAL:\n"
            f"{proposal}\n\n"
            "USER CONSTRAINTS:\n"
            f"{user_constraints}\n\n"
            "RISK CAPS (from agent_settings):\n"
            f"{risk_caps}\n\n"
            "ANALYST REPORTS:\n\n"
            + "\n\n".join(report_blocks)
            + prior_block
            + f"\n\nProduce the RiskVerdict JSON now. Set `perspective` to "
            f"{self.perspective!r} and `round_index` to {round_index}."
        )
        return system, user


__all__ = [
    "CitedConcern",
    "Perspective",
    "RiskOfficerAgent",
    "RiskVerdict",
]
