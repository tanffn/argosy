"""AnalystResponderAgent — analyst's structured response to one FM objection.

Used by the FM-objection ZigZag flow (``argosy/orchestrator/flows/
fm_objection_dialogue.py``). The Fund Manager's verdict on a draft
sometimes references one or more analyst agents by name
(``agent_report:TechnicalAnalystAgent``). Rather than tear down the
whole plan and re-run the full synthesis ($3-4, 30-70 min), the user
clicks "Discuss with TechnicalAnalyst" on a single objection and a
slim 3-turn dialogue runs:

  1. (No LLM) reformat the FM's objection text as a question.
  2. The analyst responds via THIS agent — CONCEDE / REBUT / CLARIFY.
  3. (Different agent) the FM issues a final verdict.

Per-call cost target: ~$0.10-0.20 on Sonnet. The analyst is generic —
the agent_role is ``analyst_responder`` so per-role pricing /
agent_settings overrides don't collide with the legacy analyst agents.
The original analyst role (technical / concentration / etc.) is
threaded into the prompt as context so the model adopts the right
perspective without us having to construct ten near-identical
subclasses.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


# Canonical mapping: FM-text agent class name -> analyst role string used
# elsewhere in Argosy (agent_role on AgentReport rows, ID prefixes in
# citations like "agent_report:TechnicalAnalystAgent"). This is the
# single source of truth for the dialogue feature; the API route, the
# flow, and the UI all parse against it.
ANALYST_AGENT_NAME_TO_ROLE: dict[str, str] = {
    "ConcentrationAnalystAgent": "concentration",
    "TechnicalAnalystAgent": "technical",
    "FundamentalsAnalystAgent": "fundamentals",
    "NewsAnalystAgent": "news",
    "SentimentAnalystAgent": "sentiment",
    "MacroAnalystAgent": "macro",
    "FxAnalystAgent": "fx",
    "TaxAnalystAgent": "tax",
    "HouseholdBudgetAnalystAgent": "household_budget",
    "PlanCritiqueAgent": "plan_critique",
}

# Reverse map for surfacing the "agent class name" back to the UI / FM
# verdict prompt.
ANALYST_ROLE_TO_AGENT_NAME: dict[str, str] = {
    v: k for k, v in ANALYST_AGENT_NAME_TO_ROLE.items()
}


class AnalystResponseToFM(BaseModel):
    """Structured analyst response to one Fund Manager objection."""

    stance: Literal["CONCEDE", "REBUT", "CLARIFY"] = Field(
        description=(
            "CONCEDE = FM is right; data correction / plan amendment needed. "
            "REBUT = analyst's prior position was correct; defend with evidence. "
            "CLARIFY = FM misread the analyst's analysis; explain the third reading."
        ),
    )
    reasoning_md: str = Field(
        description=(
            "Markdown body, 200-400 words. Walk through the analyst's "
            "reasoning step by step. Quote any numbers from the original "
            "objection or the prior agent_report verbatim."
        ),
    )
    suggested_fix: str = Field(
        default="",
        description=(
            "Concrete amendment to the plan, if any. Empty string when "
            "stance=REBUT or CLARIFY and no plan change is warranted. "
            "Single paragraph (no bullets) so the FM can quote it back in "
            "its final verdict."
        ),
    )
    cited_sources: list[str] = Field(
        default_factory=list,
        description=(
            "Source IDs the analyst is leaning on. Include the prior "
            "agent_report ID (e.g. 'agent_report:TechnicalAnalystAgent') "
            "so the lineage is traceable."
        ),
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM


class AnalystResponderAgent(BaseAgent[AnalystResponseToFM]):
    """Generic analyst-perspective responder, parameterized by analyst_role.

    Sonnet by default — single structured output, no extended thinking
    needed at this slim scope. ``require_citations=True`` so the
    analyst's lineage back to its prior agent_report is enforced.
    """

    agent_role = "analyst_responder"
    output_model = AnalystResponseToFM
    require_citations = True
    max_tokens = 2048

    def build_prompt(
        self,
        *,
        analyst_role: str,
        objection_topic: str,
        objection_detail: str,
        objection_severity: str,
        prior_agent_report_excerpt: str,
        prior_decision_audit_token: str,
        prior_agent_report_id: int | None,
    ) -> tuple[str, str]:
        """Build the prompt for the analyst's response.

        Args:
            analyst_role: one of the values in
                ``ANALYST_AGENT_NAME_TO_ROLE`` (e.g. "technical",
                "concentration"). Drives the persona the model adopts.
            objection_topic / detail / severity: the FM's verbatim
                concern as parsed from the FundManagerPlanRevisionDecision
                ``reasons`` list.
            prior_agent_report_excerpt: ~3-5k chars of the analyst's
                prior agent_report.response_text. The flow truncates
                large reports to keep this prompt under the per-call
                budget; we tell the model the truncation is expected.
            prior_decision_audit_token: e.g. "plan-synth-42" — the run
                that produced the prior analysis.
            prior_agent_report_id: DB id of the prior agent_report row;
                surfaced in the system prompt so the model can echo it
                in cited_sources.
        """
        agent_class_name = ANALYST_ROLE_TO_AGENT_NAME.get(
            analyst_role, f"{analyst_role.title()}Analyst",
        )

        system = (
            f"You are the {analyst_role} analyst on the Argosy fleet. "
            f"In a prior plan-synthesis run you produced an "
            f"agent_report (role={analyst_role!r}). The Fund Manager has "
            "now objected to the plan, and references your analysis as a "
            "source of the concern.\n\n"
            "Your job in this slim dialogue is to respond with one of:\n"
            "  - CONCEDE: the FM is right. Your prior analysis was wrong, "
            "incomplete, or has been overtaken by new data. Explain what "
            "should change in the plan as a result.\n"
            "  - REBUT: the FM has misjudged your prior position. Defend "
            "it with concrete evidence from the prior agent_report and "
            "cite the source IDs you used.\n"
            "  - CLARIFY: the FM has read your analysis literally where it "
            "was intended directionally (or vice versa). Offer the third "
            "reading both sides have missed — neither pure CONCEDE nor "
            "pure REBUT.\n\n"
            "Rules:\n"
            "  - Stay in your lane. You speak only for the "
            f"{analyst_role} domain — don't opine on other analysts' "
            "territory.\n"
            "  - Preserve every number the FM cited. If the FM said "
            "$200.14, repeat $200.14 in your reasoning (or explain why "
            "the FM's figure was wrong if it was).\n"
            "  - Cite the prior agent_report explicitly. Use the source "
            f"ID format 'agent_report:{agent_class_name}' so the "
            "downstream lineage tooling can trace your reasoning.\n"
            "  - If CONCEDE, the suggested_fix field MUST be populated "
            "with a concrete amendment proposal. If REBUT or CLARIFY, "
            "suggested_fix may be empty.\n"
            "  - Do NOT propose changes outside the scope of THIS one "
            "objection. The dialogue is intentionally narrow.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{AnalystResponseToFM.model_json_schema()}\n"
        )

        prior_id_line = (
            f"prior agent_report DB id: {prior_agent_report_id}"
            if prior_agent_report_id is not None
            else "prior agent_report DB id: (unknown — flow did not provide one)"
        )

        user = (
            "The Fund Manager raised this concern about your prior analysis:\n\n"
            f"=== FM OBJECTION ({objection_severity}) ===\n"
            f"TOPIC: {objection_topic}\n"
            f"DETAIL: {objection_detail}\n\n"
            f"=== YOUR PRIOR AGENT_REPORT (excerpt) ===\n"
            f"From synthesis run: {prior_decision_audit_token}\n"
            f"{prior_id_line}\n\n"
            f"{prior_agent_report_excerpt or '(no prior report excerpt available; respond from first principles in your role)'}\n\n"
            "Now respond to the FM. Pick exactly one stance "
            "(CONCEDE / REBUT / CLARIFY). Produce the AnalystResponseToFM "
            "JSON now."
        )
        return system, user


__all__ = [
    "ANALYST_AGENT_NAME_TO_ROLE",
    "ANALYST_ROLE_TO_AGENT_NAME",
    "AnalystResponderAgent",
    "AnalystResponseToFM",
]
