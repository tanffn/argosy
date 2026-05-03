"""Argosy agent fleet.

Phase 1: intake, plan-critique.
Phase 2: news, macro, concentration analysts.
Phase 3: bull/bear researchers + facilitator, trader, 3-perspective risk
team + facilitator, fund manager.

The public abstraction the rest of Argosy code touches is `BaseAgent`.
"""

from __future__ import annotations

from argosy.agents.base import AgentReport, BaseAgent, ConfidenceBand
from argosy.agents.concentration_analyst import (
    Breach,
    ConcentrationAnalystAgent,
    ConcentrationReport,
    NvdaPace,
)
from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.agents.fund_manager import FundManagerAgent, FundManagerDecision
from argosy.agents.macro_analyst import MacroAnalystAgent, MacroReport
from argosy.agents.news_analyst import Headline, NewsAnalystAgent, NewsDigest
from argosy.agents.researcher import (
    BearResearcherAgent,
    BullResearcherAgent,
    CitedPoint,
    ResearcherTurn,
)
from argosy.agents.researcher_facilitator import (
    DebateOutcome,
    ResearcherFacilitatorAgent,
)
from argosy.agents.risk_facilitator import RiskFacilitatorAgent, RiskOutcome
from argosy.agents.risk_officer import (
    CitedConcern,
    Perspective,
    RiskOfficerAgent,
    RiskVerdict,
)
from argosy.agents.trader import ExpectedImpact, TraderAgent, TraderProposal

__all__ = [
    "AgentReport",
    "BaseAgent",
    "ConfidenceBand",
    "AgentRunError",
    "MissingAPIKeyError",
    # Phase 2 analysts
    "Breach",
    "ConcentrationAnalystAgent",
    "ConcentrationReport",
    "Headline",
    "MacroAnalystAgent",
    "MacroReport",
    "NewsAnalystAgent",
    "NewsDigest",
    "NvdaPace",
    # Phase 3 decision team
    "BearResearcherAgent",
    "BullResearcherAgent",
    "CitedConcern",
    "CitedPoint",
    "DebateOutcome",
    "ExpectedImpact",
    "FundManagerAgent",
    "FundManagerDecision",
    "Perspective",
    "ResearcherFacilitatorAgent",
    "ResearcherTurn",
    "RiskFacilitatorAgent",
    "RiskOfficerAgent",
    "RiskOutcome",
    "RiskVerdict",
    "TraderAgent",
    "TraderProposal",
]
