"""Researcher facilitator agent (SDD §3.2, Phase 3).

Bounds the bull/bear debate, runs N rounds, and extracts the prevailing
position into a structured `DebateOutcome`. Default Sonnet.

Independence weighing (2026-08 stream B / TRLV scar): points marked
``independence=shared_payload`` are restatements of the shared analyst
payload and do not count as independent cross-checks. Prefer evidence
the bear (or either side) retrieved from primary sources. Optional
WebSearch lets the facilitator spot-check contested material figures
rather than converging on a shared error.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand
from argosy.agents.researcher import _render_premise_block


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
    # Stream B / TRLV — structural, not prompt-only. Record every material
    # contradiction between a researcher turn and the premise-check report
    # so the trader can see it even if synthesis prose omits it.
    premise_disagreements: list[str] = Field(
        default_factory=list,
        description="Explicit disagreements with the premise-check report "
        "(e.g. premise said pending, bear independently showed "
        "already_happened). Empty when none. MUST be populated when the "
        "bear (or bull) contradicts a premise-check status with cited "
        "independent evidence — do not suppress to force convergence.",
    )
    rounds_run: int = Field(ge=1, description="Number of full rounds completed.")
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Top-level cited sources, required for the citation gate.",
    )


class ResearcherFacilitatorAgent(BaseAgent[DebateOutcome]):
    """Facilitator. Reads the full debate transcript and verdicts.

    WebSearch is available for spot-checking contested material figures
    when both sides rest on the same shared payload (accuracy over cost).
    """

    agent_role = "researcher_facilitator"
    output_model = DebateOutcome
    require_citations = True
    claude_code_allowed_tools: ClassVar[tuple[str, ...]] = ("WebSearch",)
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (16000).

    def build_prompt(
        self,
        *,
        bull_turns: list[dict],
        bear_turns: list[dict],
        rounds_run: int,
        ticker: str = "",
        user_directive: str = "",
        premise_status: dict | None = None,
    ) -> tuple[str, str]:
        """Build the prompt.

        Args:
            bull_turns: list of `ResearcherTurn` dicts (bull side, in order).
            bear_turns: list of `ResearcherTurn` dicts (bear side, in order).
            rounds_run: total rounds completed.
            ticker: optional informational header.
            premise_status: optional premise-check report dict.
        """
        system = (
            "You are the researcher facilitator on the Argosy fleet. You "
            "have read every round of the bull/bear debate. Your job is to "
            "issue a structured verdict on which side prevailed.\n\n"
            "Rules:\n"
            "  - 'bull' wins iff the bull case carried with cited evidence "
            "the bear could not refute. 'bear' wins symmetrically. 'split' "
            "if neither side dominated or if both rely on contested data.\n"
            "  - INDEPENDENCE WEIGHING: each cited point may carry "
            "`independence` = `independent` (derived: cites a URL observed "
            "in that researcher's WebSearch tool results) or `shared_payload` "
            "(restatement of the shared analyst reports). Weigh `independent` "
            "evidence ABOVE `shared_payload` restatement. A bear point that "
            "only cites fundamentals/TICKER (or other shared-payload ids) is "
            "NOT an independent cross-check — do not treat agreement between "
            "bull and bear on a shared-payload figure as confirmation.\n"
            "  - When both sides rest on the same shared figure and it is "
            "material to the verdict, you MAY use WebSearch to spot-check "
            "the figure against a primary source rather than converging on "
            "a shared error. Cite any URL you use.\n"
            "  - PREMISE CHECK (when present) is contestable evidence from "
            "another agent, NOT ground truth. If the bear independently "
            "retrieves a primary source that contradicts the premise check "
            "(e.g. premise says pending, bear cites already_happened), "
            "preserve that disagreement in `premise_disagreements` AND in "
            "your synthesis — do not suppress it to force convergence on "
            "the premise-check framing.\n"
            "  - Synthesis is the prevailing thesis in 2-4 sentences. Be "
            "concrete; the trader will act on this.\n"
            "  - Cite specific evidence strings from the winning side; "
            "preserve the source citations attached to each.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{DebateOutcome.model_json_schema()}\n"
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
                "from the prior round. Where the user has resolved a debate "
                "point, defer to the user's resolution; don't fork the "
                "synthesizer further on items the user has already settled.\n"
                "  - AGREED items: accept the user's resolution as the "
                "prevailing outcome on that point; reflect it in the synthesis.\n"
                "  - DISAGREED items: when the user supplied a counter-position, "
                "treat that as the directional verdict on that point.\n"
                "  - DEFERRED items: judge on the debate's merits.\n"
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

        premise_prefix = _render_premise_block(premise_status)

        user = (
            f"{directive_prefix}"
            f"{premise_prefix}"
            f"Ticker under debate: {ticker or '(unspecified)'}\n"
            f"Rounds run: {rounds_run}\n\n"
            "=== BULL SIDE ===\n" + _render(bull_turns, "bull") + "\n\n"
            "=== BEAR SIDE ===\n" + _render(bear_turns, "bear") + "\n\n"
            f"Set `rounds_run` to {rounds_run}. Produce the DebateOutcome JSON now."
        )
        return system, user


__all__ = ["DebateOutcome", "ResearcherFacilitatorAgent"]
