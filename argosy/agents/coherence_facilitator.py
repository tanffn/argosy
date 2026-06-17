# argosy/agents/coherence_facilitator.py
"""Reads panelist positions; reports consensus + the agreed ruling, or no-consensus
+ the crux. Mirrors risk_facilitator. Does NOT impose a ruling on no-consensus —
that escalates to the arbitrator."""
from __future__ import annotations

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent


class FacilitatorOutcome(BaseModel):
    consensus: bool = Field(description="True if panelists agree.")
    ruling: str = Field(default="", description="The agreed ruling when consensus is True.")
    crux: str = Field(default="", description="The core disagreement when consensus is False.")


class CoherenceFacilitatorAgent(BaseAgent[FacilitatorOutcome]):
    agent_role = "coherence_facilitator"
    output_model = FacilitatorOutcome
    require_citations = False

    def build_prompt(self, *, dispute_question: str, positions: list[dict]) -> tuple[str, str]:
        system = (
            "You facilitate a coherence panel. Determine whether the panelists agree. "
            "If they do, state the agreed ruling. If not, state the crux of the "
            "disagreement crisply. Do NOT invent a ruling on no-consensus."
        )
        lines = "\n".join(
            f"  - {p.get('role','?')}: {p.get('position','')} (basis={p.get('basis','')})"
            for p in positions
        )
        user = f"DISPUTED QUESTION:\n{dispute_question}\n\nPOSITIONS:\n{lines}\n"
        return system, user
