"""ObjectionTranslatorAgent — render a verbose Fund Manager objection in plain English.

Sonnet-class summarizer. Takes the FM's terse, jargon-heavy objection
(``topic + detail``) and returns:

- ``headline``: one short sentence stating the problem
- ``plain_english``: 2-4 paragraphs explaining the math/logic the FM
  used, in language a non-expert can follow
- ``recommended_actions``: list of 1-3 concrete options the user can
  pick from

Used by ``/api/plan/draft/objections/translate`` to power the "Explain
in plain English" button on each FM objection card.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class ObjectionTranslation(BaseModel):
    headline: str = Field(
        default="",
        description=(
            "One short sentence (<=120 chars) summarising the problem. "
            "Plain language, no jargon."
        ),
    )
    plain_english: str = Field(
        default="",
        description=(
            "Body paragraph(s) explaining the FM's reasoning step by step. "
            "Walk through any numbers from the original detail so the user "
            "can verify them. 200-400 words target."
        ),
    )
    recommended_actions: list[str] = Field(
        default_factory=list,
        description=(
            "1-3 concrete options the user (or the synthesizer on re-run) "
            "could take to resolve the objection. Each option is a "
            "single sentence."
        ),
    )
    confidence: ConfidenceBand = ConfidenceBand.HIGH
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Echo of any source IDs from the original detail.",
    )


class ObjectionTranslatorAgent(BaseAgent[ObjectionTranslation]):
    """Translate a verbose FM objection into plain English.

    Single-purpose Sonnet-class agent. Reads the FM's verbose
    ``topic + detail`` text, produces an ``ObjectionTranslation``. No
    external state — pure text-in, structured-text-out.

    Citations are echoed from the input (the FM's cited_sources) rather
    than re-derived: the translator is a paraphrasing layer, not a new
    judgment. ``require_citations`` is set False because the structured
    output already includes the cited list explicitly.
    """

    agent_role = "objection_translator"
    output_model = ObjectionTranslation
    require_citations = False
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (8000).

    def build_prompt(
        self,
        *,
        topic: str,
        detail: str,
        severity: str,
        cited_sources: list[str] | None = None,
    ) -> tuple[str, str]:
        """Build prompt from one FM objection.

        Args:
            topic: short headline label from the FM ("COHERENCE FAILURE",
                "HARD TAX GATE NOT EMBEDDED IN PLAN", etc.)
            detail: the verbose body the FM emitted.
            severity: RED / AMBER / YELLOW.
            cited_sources: list of source IDs the FM cited; echoed in
                the output unchanged.
        """
        sources_block = ""
        if cited_sources:
            sources_block = (
                "\n\nCited sources (echo these in your output verbatim):\n"
                + "\n".join(f"  - {s}" for s in cited_sources)
            )

        system = (
            "You are the objection translator on the Argosy fleet. Your "
            "job: take a Fund Manager's terse rejection note, often packed "
            "with jargon, ticker arithmetic, regulatory references, and "
            "internal source IDs, and translate it into language a user "
            "WITHOUT a finance background can act on.\n\n"
            "Rules:\n"
            "  - Preserve every NUMBER from the original. If the FM cited "
            "    10,677 shares, show 10,677 shares. Don't round or "
            "    summarise away.\n"
            "  - Walk through the FM's chain of reasoning step by step. "
            "    Where the FM does math, show the math.\n"
            "  - Translate jargon (Section 102, RNA estate tax, etc.) "
            "    with a one-line aside on what the term means.\n"
            "  - End with 1-3 RECOMMENDED ACTIONS the user can choose "
            "    from — concrete and specific. Don't recommend more than "
            "    3; pick the strongest.\n"
            "  - Do NOT add new opinions or facts the FM didn't already "
            "    state. You're paraphrasing, not adjudicating.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{ObjectionTranslation.model_json_schema()}\n"
        )

        user = (
            f"SEVERITY: {severity}\n"
            f"FM OBJECTION TOPIC: {topic}\n"
            f"FM OBJECTION DETAIL:\n{detail}"
            f"{sources_block}\n\n"
            "Produce the ObjectionTranslation JSON now."
        )
        return system, user


__all__ = ["ObjectionTranslatorAgent", "ObjectionTranslation"]
