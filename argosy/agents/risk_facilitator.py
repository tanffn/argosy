"""Risk facilitator agent (SDD §3.4, Phase 3).

Reads the 3-perspective debate transcript, runs N rounds, and extracts
a `RiskOutcome` with consensus verdict and consolidated conditions.
Default Sonnet.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class RiskOutcome(BaseModel):
    """Facilitator's verdict on the risk debate."""

    consensus_verdict: Literal[
        "APPROVE", "APPROVE_WITH_CONDITIONS", "REJECT", "ESCALATE"
    ] = Field(
        description="Consensus across the 3 perspectives. ESCALATE means "
        "no consensus; the fund manager (and ultimately the user) decides."
    )
    consolidated_conditions: list[str] = Field(
        default_factory=list,
        description="Union of conditions raised by any officer that the "
        "facilitator deems material; usually the strictest conditions win.",
    )
    dissent_summary: str = Field(
        default="",
        description="Short summary of the strongest dissenting view (the "
        "officer who voted differently from the consensus).",
    )
    rounds_run: int = Field(ge=1)
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Top-level distinct cited sources, required for the gate.",
    )


class RiskFacilitatorAgent(BaseAgent[RiskOutcome]):
    """Sonnet-class facilitator. Reads the 3-perspective debate."""

    agent_role = "risk_facilitator"
    output_model = RiskOutcome
    require_citations = True
    max_tokens = 2048

    def build_prompt(
        self,
        *,
        verdicts: list[dict],
        rounds_run: int,
    ) -> tuple[str, str]:
        """Build the prompt.

        Args:
            verdicts: list of `RiskVerdict` dicts from all officers across
                all rounds (chronological).
            rounds_run: total rounds completed.
        """
        system = (
            "You are the risk facilitator on the Argosy fleet. You have "
            "read every round of the 3-perspective risk debate. Your job "
            "is to issue a structured verdict on the consensus.\n\n"
            "Rules:\n"
            "  - APPROVE iff at least 2 of 3 perspectives APPROVE in the "
            "final round AND no one REJECTs.\n"
            "  - APPROVE_WITH_CONDITIONS iff the strictest passing verdict "
            "carries non-empty conditions; consolidate them.\n"
            "  - REJECT iff at least 2 of 3 REJECT.\n"
            "  - ESCALATE iff there is no clear majority (e.g., split 1/1/1).\n"
            "  - Conservative-officer concerns about capital preservation "
            "in T3 contexts always escalate to the fund manager.\n"
            "  - Cite the perspectives whose verdicts drive the consensus.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{RiskOutcome.model_json_schema()}\n"
        )

        chunks: list[str] = []
        for v in verdicts:
            p = v.get("perspective", "?")
            r = v.get("round_index", "?")
            verdict = v.get("verdict", "?")
            conditions = v.get("conditions", [])
            concerns = v.get("concerns", [])
            chunks.append(
                f"  Round {r} — {p} → {verdict}\n"
                f"    conditions: {conditions}\n"
                f"    concerns:   {concerns}"
            )

        user = (
            f"Rounds run: {rounds_run}\n"
            f"Total verdicts: {len(verdicts)}\n\n"
            "RISK DEBATE TRANSCRIPT:\n"
            + "\n".join(chunks)
            + f"\n\nSet `rounds_run` to {rounds_run}. Produce the "
            "RiskOutcome JSON now."
        )
        return system, user


__all__ = ["RiskFacilitatorAgent", "RiskOutcome"]
