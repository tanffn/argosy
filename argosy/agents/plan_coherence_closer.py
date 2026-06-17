"""PlanCoherenceCloserAgent — the agent that CLOSES reader contradictions.

The surgical prose editor fixes one cited snippet at a time, blind to the other
surface of the same contradiction, so it whack-a-moles (fix the equity-comp
policy, the action list still disagrees; the reader re-flags). This agent
instead sees the WHOLE draft + ALL the reader's findings + the canonical resolver
facts at once, and emits a mutually-consistent set of find/replace edits that fix
EVERY surface of each contradiction together — grounded only in the canonical
numbers. Used in a zigzag: reader finds -> closer reconciles -> reader re-reads,
bounded, until APPROVE / APPROVE_WITH_CONDITIONS.

Argosy-native: an agent closing the loop, not a post-hoc string patch.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent


class CoherenceEdit(BaseModel):
    """One exact find/replace edit in a horizon body, part of a globally-
    consistent set."""

    horizon: Literal["long", "medium", "short"] = Field(
        description="Which horizon body the `find` text lives in.",
    )
    find: str = Field(
        description="EXACT verbatim substring currently in that horizon body to "
        "replace (copy it precisely, including punctuation).",
    )
    replace: str = Field(
        description="The corrected text. Must be CONSISTENT with every other edit "
        "and with the canonical facts. Use ONLY numbers from the canonical facts "
        "or already in `find`; never invent a number/ticker; never mention a prior "
        "plan, revision history, or internal tokens.",
    )
    fixes_finding: int = Field(
        description="Index of the reader finding this edit (with its siblings) "
        "resolves.",
    )


class CoherenceClose(BaseModel):
    """A round of globally-consistent edits closing the reader's findings."""

    edits: list[CoherenceEdit] = Field(default_factory=list)
    notes: str = Field(default="", description="One line on how the contradictions were reconciled.")


class PlanCoherenceCloserAgent(BaseAgent[CoherenceClose]):
    """Reconcile ALL reader contradictions across ALL surfaces in one coherent
    pass. Opus by default (this is the high-value coherence step; accuracy over
    cost). No citations (it edits an existing doc against canonical facts)."""

    agent_role = "plan_coherence_closer"
    output_model = CoherenceClose
    require_citations = False

    def build_prompt(
        self,
        *,
        findings_block: str,
        canonical_facts: str,
        long_md: str,
        medium_md: str,
        short_md: str,
    ) -> tuple[str, str]:
        system = (
            "You are the Argosy plan coherence closer. An adversarial whole-artifact "
            "reader found CONTRADICTIONS across the plan's surfaces (the same fact "
            "stated differently in different sections). Your job: make every surface "
            "AGREE.\n\n"
            "RULES:\n"
            "- For EACH finding, fix EVERY surface of the contradiction in this one "
            "response (e.g. if the equity-comp section and the action list disagree, "
            "edit BOTH so they state the same thing). Half-fixes get re-flagged.\n"
            "- Ground every value in the CANONICAL FACTS below. Use ONLY numbers that "
            "appear in the canonical facts or already in the text you are replacing. "
            "NEVER invent a number, percentage, ticker, or date.\n"
            "- Resolve a fragile/thin-margin claim by stating it HONESTLY (do not "
            "delete it, do not overclaim). Resolve distinct concepts (e.g. earliest-"
            "safe age vs full-FI/bridge age) by LABELING each clearly, not by forcing "
            "them equal.\n"
            "- Never write 'prior plan' / 'previous version' / revision history / "
            "internal tokens like '[domain_knowledge...]' / 'agent_report:'.\n"
            "- Make the SMALLEST edits that achieve consistency. `find` must be an "
            "EXACT verbatim substring of the named horizon body.\n"
            "Return a set of find/replace edits across the horizons that, applied "
            "together, leave the plan internally consistent."
        )
        user = (
            f"CANONICAL FACTS (the single source of truth — every surface must match these):\n"
            f"{canonical_facts}\n\n"
            f"READER FINDINGS TO CLOSE (resolve every one, on all surfaces):\n"
            f"{findings_block}\n\n"
            f"=== LONG HORIZON BODY ===\n{long_md}\n\n"
            f"=== MEDIUM HORIZON BODY ===\n{medium_md}\n\n"
            f"=== SHORT HORIZON BODY ===\n{short_md}\n"
        )
        return system, user
