"""Researcher facilitator agent (SDD §3.2, Phase 3).

Bounds the bull/bear debate, runs N rounds, and extracts the prevailing
position into a structured `DebateOutcome`. Default Sonnet.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class DebateOutcome(BaseModel):
    """Facilitator's verdict on the bull/bear debate."""

    winning_side: Literal["bull", "bear", "split"] = Field(
        description="The side whose case prevailed; 'split' if neither dominated."
    )
    synthesis: str = Field(
        description="2-4 sentence synthesis of the prevailing thesis. Drives "
        "the trader's framing in the next stage."
    )
    cited_evidence: list[str] = Field(
        default_factory=list,
        description="Distinct cited evidence strings carried over from the "
        "winning case; used by the trader and the fund manager downstream.",
    )
    rounds_run: int = Field(ge=1, description="Number of full rounds completed.")
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Top-level cited sources, required for the citation gate.",
    )


class ResearcherFacilitatorAgent(BaseAgent[DebateOutcome]):
    """Sonnet-class facilitator. Reads the full debate transcript and verdicts."""

    agent_role = "researcher_facilitator"
    output_model = DebateOutcome
    require_citations = True
    max_tokens = 2048

    def build_prompt(
        self,
        *,
        bull_turns: list[dict],
        bear_turns: list[dict],
        rounds_run: int,
        ticker: str = "",
    ) -> tuple[str, str]:
        """Build the prompt.

        Args:
            bull_turns: list of `ResearcherTurn` dicts (bull side, in order).
            bear_turns: list of `ResearcherTurn` dicts (bear side, in order).
            rounds_run: total rounds completed.
            ticker: optional informational header.
        """
        system = (
            "You are the researcher facilitator on the Argosy fleet. You "
            "have read every round of the bull/bear debate. Your job is to "
            "issue a structured verdict on which side prevailed.\n\n"
            "Rules:\n"
            "  - 'bull' wins iff the bull case carried with cited evidence "
            "the bear could not refute. 'bear' wins symmetrically. 'split' "
            "if neither side dominated or if both rely on contested data.\n"
            "  - Synthesis is the prevailing thesis in 2-4 sentences. Be "
            "concrete; the trader will act on this.\n"
            "  - Cite specific evidence strings from the winning side; "
            "preserve the source citations attached to each.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{DebateOutcome.model_json_schema()}\n"
        )

        def _render(turns: list[dict], side: str) -> str:
            if not turns:
                return f"({side}: no turns)"
            chunks = []
            for t in turns:
                idx = t.get("round_index", "?")
                ps = t.get("position_summary", "")
                pts = t.get("points", [])
                chunks.append(
                    f"  Round {idx}: {ps}\n  Points: {pts}"
                )
            return "\n".join(chunks)

        user = (
            f"Ticker under debate: {ticker or '(unspecified)'}\n"
            f"Rounds run: {rounds_run}\n\n"
            "=== BULL SIDE ===\n" + _render(bull_turns, "bull") + "\n\n"
            "=== BEAR SIDE ===\n" + _render(bear_turns, "bear") + "\n\n"
            f"Set `rounds_run` to {rounds_run}. Produce the DebateOutcome JSON now."
        )
        return system, user


__all__ = ["DebateOutcome", "ResearcherFacilitatorAgent"]
