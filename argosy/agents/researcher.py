"""Researcher agents — bull and bear (SDD §3.2, Appendix B.2, Phase 3).

Adversarial debate. Each side reads the analyst reports + the prior
debate rounds and marshals the strongest case from the evidence. Both
sides default to Opus per SDD §3.8 (adversarial debate is exactly the
case that justifies the spend).

The output is a `ResearcherTurn`: the position summary, 3-5 cited
points, and a direct response to the strongest opposing point from the
prior round (empty for round 1).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class CitedPoint(BaseModel):
    """One argument with its supporting evidence."""

    claim: str = Field(description="One sentence stating the argument.")
    evidence: str = Field(
        description="Concrete evidence drawn from analyst reports. Should "
        "include numbers or specific quotations where possible."
    )
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Analyst-report identifiers or domain_knowledge paths "
        "supporting this claim. Required (the citation gate enforces).",
    )


class ResearcherTurn(BaseModel):
    """Output of one round from one side of the debate."""

    side: Literal["bull", "bear"]
    round_index: int = Field(ge=1, description="1-indexed round counter.")
    position_summary: str = Field(
        description="One-sentence statement of the side's overall position."
    )
    points: list[CitedPoint] = Field(
        default_factory=list,
        description="3-5 strongest cited points the side advances this round.",
    )
    response_to_opposing: str = Field(
        default="",
        description="Direct response to the strongest opposing point from "
        "the prior round; empty string for round 1.",
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Top-level distinct cited sources across all points; "
        "required for the citation gate.",
    )


class _ResearcherAgent(BaseAgent[ResearcherTurn]):
    """Shared base. Concrete subclasses set `_side` to 'bull' or 'bear'."""

    output_model = ResearcherTurn
    require_citations = True
    max_tokens = 3072

    _side: ClassVar[Literal["bull", "bear"]] = "bull"

    def build_prompt(
        self,
        *,
        analyst_reports: list[dict],
        prior_rounds: list[dict] | None = None,
        round_index: int = 1,
        n_max: int = 2,
        ticker: str = "",
    ) -> tuple[str, str]:
        """Build the prompt for one debate turn.

        Args:
            analyst_reports: list of analyst report payloads (already
                serialized to plain dicts via pydantic's `model_dump`).
                Each dict carries at minimum `agent_role` and the report
                content.
            prior_rounds: list of prior `ResearcherTurn` dicts in
                chronological order (both sides interleaved). Empty for
                round 1.
            round_index: 1-based round number.
            n_max: total planned rounds (informational; included in prompt
                so the agent can pace its argument).
            ticker: the ticker under debate; informational header.
        """
        prior_rounds = prior_rounds or []
        opposite = "bear" if self._side == "bull" else "bull"

        system = (
            f"You are the {self._side} researcher on the Argosy fleet. "
            f"You marshal the strongest possible {self._side}ish case from the "
            "evidence in the analyst reports. The other side argues the "
            "opposite case.\n\n"
            "Rules:\n"
            "  - Cite analyst reports and specific facts. Do NOT invent facts.\n"
            "  - Address the strongest opposing point from the prior round; "
            "if this is round 1, leave `response_to_opposing` empty.\n"
            "  - Length: 200-400 words across the points.\n"
            "  - Each point must carry evidence and at least one citation.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{ResearcherTurn.model_json_schema()}\n"
        )

        # Render analyst reports
        report_blocks: list[str] = []
        for r in analyst_reports:
            role = r.get("agent_role") or r.get("role") or "?"
            payload = {k: v for k, v in r.items() if k not in ("agent_role", "role")}
            report_blocks.append(f"### Analyst: {role}\n{payload}")

        prior_block = ""
        if prior_rounds:
            chunks: list[str] = []
            for i, t in enumerate(prior_rounds, start=1):
                side = t.get("side", "?")
                summary = t.get("position_summary", "")
                points = t.get("points", [])
                chunks.append(
                    f"--- prior turn #{i} ({side}) ---\n"
                    f"summary: {summary}\n"
                    f"points: {points}"
                )
            prior_block = (
                "\n\nPRIOR DEBATE ROUNDS (most recent last):\n"
                + "\n".join(chunks)
                + f"\n\nThe LAST {opposite} turn is the one you must respond to."
            )

        user = (
            f"Ticker under debate: {ticker or '(unspecified)'}\n"
            f"Round {round_index} of {n_max}; you argue the {self._side} case.\n\n"
            "ANALYST REPORTS:\n\n" + "\n\n".join(report_blocks)
            + prior_block
            + "\n\nProduce the ResearcherTurn JSON now. Set `side` to "
            f"{self._side!r} and `round_index` to {round_index}."
        )
        return system, user


class BullResearcherAgent(_ResearcherAgent):
    """Bull-side researcher. Default Opus."""

    agent_role = "bull_researcher"
    _side = "bull"


class BearResearcherAgent(_ResearcherAgent):
    """Bear-side researcher. Default Opus."""

    agent_role = "bear_researcher"
    _side = "bear"


__all__ = [
    "BearResearcherAgent",
    "BullResearcherAgent",
    "CitedPoint",
    "ResearcherTurn",
]
