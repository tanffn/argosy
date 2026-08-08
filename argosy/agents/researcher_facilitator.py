"""Researcher facilitator agent (SDD §3.2, Phase 3).

Bounds the bull/bear debate, runs N rounds, and extracts the prevailing
position into a structured `DebateOutcome`. Default Sonnet.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class FacilitatorCondition(BaseModel):
    """Machine-representable condition attached to a facilitator verdict.

    When the facilitator would otherwise write "pending a domain-refresh
    of flagged fundamentals" into prose, it MUST emit a structured
    condition instead. The orchestrator persists these as open
    ``remediation_requests`` rows that BLOCK green_light until cleared.
    Stream A — TRLV 2026-08-07 failure class.
    """

    kind: Literal[
        "domain_refresh",
        "data_refresh",
        "pending_remediation",
        "vintage_check",
    ] = Field(
        description=(
            "What must happen before the verdict may drive a green_light. "
            "domain_refresh / data_refresh = re-pull the named data; "
            "pending_remediation = an analyst-flagged integrity break "
            "must clear; vintage_check = financials must post-date earnings."
        ),
    )
    ticker: str | None = Field(
        default=None,
        description="Ticker the condition applies to (None = whole run).",
    )
    description: str = Field(
        description="One-sentence machine+human readable condition.",
    )


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
    conditions: list[FacilitatorCondition] = Field(
        default_factory=list,
        description=(
            "Blocking conditions that must clear before green_light. "
            "Empty means the verdict is unconditional. NEVER put "
            "'pending refresh' language into synthesis without also "
            "emitting a matching condition here."
        ),
    )


class ResearcherFacilitatorAgent(BaseAgent[DebateOutcome]):
    """Sonnet-class facilitator. Reads the full debate transcript and verdicts."""

    agent_role = "researcher_facilitator"
    output_model = DebateOutcome
    require_citations = True
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (16000).

    def build_prompt(
        self,
        *,
        bull_turns: list[dict],
        bear_turns: list[dict],
        rounds_run: int,
        ticker: str = "",
        user_directive: str = "",
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
            "preserve the source citations attached to each.\n"
            "  - CONDITIONS (Stream A): if your verdict depends on a "
            "data refresh, domain refresh, or clearing a flagged "
            "integrity break, emit a structured entry on ``conditions`` "
            "(kind + ticker + description). Do NOT bury that dependency "
            "only in ``synthesis`` prose — unrecorded conditions will "
            "not block green_light. Leave ``conditions`` empty when the "
            "verdict is unconditional.\n\n"
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

        user = (
            f"{directive_prefix}"
            f"Ticker under debate: {ticker or '(unspecified)'}\n"
            f"Rounds run: {rounds_run}\n\n"
            "=== BULL SIDE ===\n" + _render(bull_turns, "bull") + "\n\n"
            "=== BEAR SIDE ===\n" + _render(bear_turns, "bear") + "\n\n"
            f"Set `rounds_run` to {rounds_run}. Produce the DebateOutcome JSON now."
        )
        return system, user


__all__ = [
    "DebateOutcome",
    "FacilitatorCondition",
    "ResearcherFacilitatorAgent",
]
