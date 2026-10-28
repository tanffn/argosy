"""Analysis-only agenda and decorrelated challenge for a small investment pilot."""
from __future__ import annotations

import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent
from argosy.services.high_potential_sleeve import X10_SLEEVE_MANDATE


class ResearchChoice(BaseModel):
    ticker: str
    why_now: str
    decisive_question: str


class ResearchAgenda(BaseModel):
    choices: list[ResearchChoice] = Field(min_length=3, max_length=3)
    alternatives_not_selected: list[str]
    rationale: str


class OpportunitySelectorAgent(BaseAgent[ResearchAgenda]):
    agent_role = "opportunity_pilot_selector"
    output_model = ResearchAgenda
    require_citations = False
    claude_code_force_toolless = True
    use_structured_output = True
    schema_retry_attempts = 2

    def build_prompt(self, *, payload):
        return (
            "Select THREE current research investigations, not three purchases. "
            + X10_SLEEVE_MANDATE + "\nUse only the supplied active candidate universe. "
            "Balance actionable uncertainty, business mechanism, current valuation, dated catalysts, "
            "portfolio contribution and correlated risk. Radar rank is not buy conviction. "
            "Past gains are not foresight evidence. Earlier WATCHs may merit resolving a specific "
            "missing fact; do not just repeat a completed review without a decisive new question. "
            "Explain which attractive alternatives you omitted and why. Three reviews may yield zero buys.",
            json.dumps(payload, ensure_ascii=False, default=str),
        )


class PilotScenario(BaseModel):
    label: Literal["downside", "base", "upside"]
    months: Literal[6, 12, 24]
    terminal_price_usd: float = Field(gt=0, allow_inf_nan=False)
    assumptions: str


class InvestmentView(BaseModel):
    verdict: Literal["BUY", "WATCH", "PASS"]
    conviction: Literal["HIGH", "MED", "LOW"]
    rationale: str
    benchmark_opportunity_cost: str
    falsifiers: list[str]
    next_review_date: date
    unresolved_questions: list[str]
    scenarios: list[PilotScenario]
    reference_price_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    reference_price_source: str | None = None


class EvidenceFinding(BaseModel):
    question: str
    status: Literal["verified", "inaccessible", "not_found", "future_disclosure", "unresolved"]
    finding: str
    source_urls: list[str]
    publication_dates: list[str]
    extraction_limitations: str


class EvidenceFollowup(BaseModel):
    priority_rationale: str
    findings: list[EvidenceFinding]
    remaining_questions: list[str]


class OpportunityEvidenceAgent(BaseAgent[EvidenceFollowup]):
    """One bounded public-document pass, never a purchase decision."""

    agent_role = "opportunity_pilot_evidence"
    output_model = EvidenceFollowup
    require_citations = False  # Explicit source URLs and limitations are in the typed findings.
    use_structured_output = True
    schema_retry_attempts = 2
    claude_code_allowed_tools = ("WebSearch", "WebFetch")
    claude_code_max_turns = 12
    claude_code_keep_tool_stream_open = True
    claude_code_public_documents = True

    def build_prompt(self, *, payload):
        from argosy.agents.research_guidance import targeted_research_guidance

        return (
            "You close material evidence gaps for an investment research team, not decide a trade. "
            "Choose at most THREE questions from the supplied unresolved list whose answers could "
            "most change the thesis and can be researched NOW. Explain that prioritization. "
            "Read the actual primary documents, preferably exact issuer/SEC links already identified. "
            "Use mcp__argosy_sources__read_document for each primary document used to resolve a "
            "question so the exact inspected text and extraction failures have durable receipts. "
            "Label verified only when inspected primary text supports it, with nonempty URLs and "
            "publication dates. Retrieval proves access, not truth; remaining inferences stay explicit. "
            "Separate unread-but-available from future disclosures and inaccessible documents. "
            "Do not treat search snippets, a remembered number or a quoted analyst as verified facts. "
            "Report document dates, URLs, extraction limits and precise unresolved points. "
            "Do not supply a verdict, allocation or invented probability. Do not reproduce household "
            "details in searches. No login, credential collection or external mutation. "
            + targeted_research_guidance("Resolve the prioritized evidence gaps in the payload."),
            json.dumps(payload, ensure_ascii=False, default=str),
        )


class OpportunityChallengerAgent(BaseAgent[InvestmentView]):
    agent_role = "opportunity_pilot_challenger"
    output_model = InvestmentView
    require_citations = False
    claude_code_force_toolless = True
    use_structured_output = True
    schema_retry_attempts = 2

    def build_prompt(self, *, payload):
        return (
            "Independently derive an investment view from dated raw analyst evidence and the "
            "household's portfolio context. You have NOT been given the discovery grader's verdict. "
            + X10_SLEEVE_MANDATE + "\nChallenge the strongest bullish AND bearish mechanisms. "
            "Separate verified facts, analyst assumptions and unknowns. Analyst fair values are opinions, "
            "not primary sources. A current P/E times the same EPS is NOT independent valuation. "
            "Inspect post-quarter financing, dilution, ADS units, earnings quality and cash conversion. "
            "Consider overlapping employer/AI/country risks; do not invent holdings or current facts. "
            "Explain buy now versus waiting or the benchmark. Do not require a fixed return multiple "
            "or force a starter. No invented calibrated probabilities. Scenario endpoints are explicit "
            "assumptions, not price targets verified by a source. Missing evidence may warrant WATCH. "
            "Record dated falsifiers and next review. No order or actual allocation is authorized.",
            json.dumps(payload, ensure_ascii=False, default=str),
        )


class OpportunityResolutionAgent(OpportunityChallengerAgent):
    agent_role = "opportunity_pilot_resolution"

    def build_prompt(self, *, payload):
        system, user = super().build_prompt(payload=payload)
        return (
            system.replace("You have NOT been given the discovery grader's verdict.",
                           "You now have the discovery grader and the independently blind challenger. "
                           "Re-derive their material disagreements from the raw evidence. Do not vote, "
                           "average verdicts or smooth away contradictions. An unresolved material "
                           "input conflict remains explicit with research questions, not a fabricated BUY. "
                           "Keep the strongest justified conclusion; do not default to caution just "
                           "because the agents differ. This is the final research view, not an order."),
            user,
        )
