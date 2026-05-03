"""Argosy agent fleet.

Phase 1: intake, plan-critique.
Phase 2: news, macro, concentration analysts.
Phase 3: bull/bear researchers + facilitator, trader, 3-perspective risk
team + facilitator, fund manager.
Phase 7: fundamentals, technical, sentiment, tax, fx analysts;
domain-refresh, audit, watchlist cross-cutting agents.

The public abstraction the rest of Argosy code touches is `BaseAgent`.
"""

from __future__ import annotations

from argosy.agents.audit_agent import AuditAgent, AuditReport, Finding as AuditFinding
from argosy.agents.base import AgentReport, BaseAgent, ConfidenceBand
from argosy.agents.concentration_analyst import (
    Breach,
    ConcentrationAnalystAgent,
    ConcentrationReport,
    NvdaPace,
)
from argosy.agents.domain_refresh import (
    CitedSource,
    DomainRefreshAgent,
    DomainRefreshReport,
    FileRefreshResult,
)
from argosy.agents.errors import AgentRunError, MissingAPIKeyError
from argosy.agents.fund_manager import FundManagerAgent, FundManagerDecision
from argosy.agents.fundamentals_analyst import (
    FundamentalsAnalystAgent,
    FundamentalsReport,
    TickerFundamentals,
)
from argosy.agents.fx_analyst import (
    FXAnalystAgent,
    FXReport,
    PairLevels,
)
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
from argosy.agents.sentiment_analyst import (
    SentimentAnalystAgent,
    SentimentReport,
    TickerSentiment,
)
from argosy.agents.tax_analyst import (
    DividendTaxProjection,
    RsuVestEstimate,
    TaxAnalystAgent,
    TaxReport,
    TLHCandidate,
)
from argosy.agents.technical_analyst import (
    TechnicalAnalystAgent,
    TechnicalReport,
    TickerTechnicals,
)
from argosy.agents.trader import ExpectedImpact, TraderAgent, TraderProposal
from argosy.agents.watchlist import (
    WatchlistAgent,
    WatchlistEntry,
    WatchlistReport,
)

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
    # Phase 7 analysts
    "FundamentalsAnalystAgent",
    "FundamentalsReport",
    "TickerFundamentals",
    "TechnicalAnalystAgent",
    "TechnicalReport",
    "TickerTechnicals",
    "SentimentAnalystAgent",
    "SentimentReport",
    "TickerSentiment",
    "TaxAnalystAgent",
    "TaxReport",
    "TLHCandidate",
    "DividendTaxProjection",
    "RsuVestEstimate",
    "FXAnalystAgent",
    "FXReport",
    "PairLevels",
    # Phase 7 cross-cutting
    "DomainRefreshAgent",
    "DomainRefreshReport",
    "FileRefreshResult",
    "CitedSource",
    "AuditAgent",
    "AuditReport",
    "AuditFinding",
    "WatchlistAgent",
    "WatchlistReport",
    "WatchlistEntry",
]
