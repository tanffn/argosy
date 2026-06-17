# argosy/agents/coherence_panelist.py
"""A panelist representing one surface-owning role. States its position on a single
disputed question, grounded in the prime directive / user directives / canonical
facts, having seen the peers' positions. Pure opinion — it does not rule."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent


class PanelistPosition(BaseModel):
    position: str = Field(description="This role's position on the disputed question.")
    basis: Literal["prime_directive", "user_directive", "canonical_fact", "preference"] = Field(
        description="The highest-authority basis backing the position."
    )
    cites: list[str] = Field(default_factory=list, description="Canonical fact / directive ids.")
    concede: bool = Field(default=False, description="True if conceding to a peer position.")


class CoherencePanelistAgent(BaseAgent[PanelistPosition]):
    agent_role = "coherence_panelist"
    output_model = PanelistPosition
    require_citations = False
    schema_retry_attempts = 2  # self-correct if the model mis-shapes a field

    def build_prompt(
        self, *, represented_role: str, dispute_question: str,
        canonical_facts: str, peer_positions: list[str],
    ) -> tuple[str, str]:
        system = (
            "You are a coherence-deliberation panelist representing the "
            f"'{represented_role}' perspective. State your position on ONE disputed "
            "question, grounded ONLY in the prime directive, the user's directives, or "
            "the canonical facts below. You may concede to a peer if their basis "
            "outranks yours. You do NOT rule — you argue your surface's view."
        )
        peers = "\n".join(f"  - {p}" for p in peer_positions) or "  (none yet)"
        user = (
            f"ROLE: {represented_role}\n\n"
            f"DISPUTED QUESTION:\n{dispute_question}\n\n"
            f"CANONICAL FACTS:\n{canonical_facts}\n\n"
            f"PEER POSITIONS:\n{peers}\n"
        )
        return system, user
