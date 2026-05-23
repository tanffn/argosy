"""DecisionFlow end-to-end tests — T0/T1/T2/T3 happy paths with all agents mocked.

Verifies:
  - tier-conditional pipeline: T0 has no debate / no risk team;
    T1 has 1-round bull/bear + neutral risk; T2/T3 has full stack.
  - persistence: agent_reports rows accumulate, decision_runs row links
    proposal id, proposals row gets created and history rows are present.
  - blocking paths: trader hold, risk reject, FM block all surface
    correctly.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from argosy.agents.base import AgentReport, ConfidenceBand, ModelCall
from argosy.agents.fund_manager import FundManagerAgent, FundManagerDecision
from argosy.agents.researcher import (
    BearResearcherAgent,
    BullResearcherAgent,
    ResearcherTurn,
)
from argosy.agents.researcher_facilitator import (
    DebateOutcome,
    ResearcherFacilitatorAgent,
)
from argosy.agents.risk_facilitator import RiskFacilitatorAgent, RiskOutcome
from argosy.agents.risk_officer import RiskOfficerAgent, RiskVerdict
from argosy.agents.trader import TraderAgent, TraderProposal
from argosy.decisions.flow import (
    ApprovedProposal,
    BlockedProposal,
    DecisionFlow,
    FlowConfig,
)
from argosy.decisions.tiers import Tier
from argosy.state import db as db_mod
from argosy.state.models import (
    AgentReport as AgentReportRow,
    DecisionRun,
    Proposal as ProposalRow,
    ProposalHistory,
    User,
)


# ----------------------------------------------------------------------
# Mock factories
# ----------------------------------------------------------------------


def _mock_agent(cls, canned: dict, **init_kwargs):
    class _M(cls):  # type: ignore[misc, valid-type]
        async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
            return ModelCall(
                text=json.dumps(canned),
                tokens_in=50,
                tokens_out=80,
                model=self.model,
            )
    return _M


_BULL_CANNED = {
    "side": "bull",
    "round_index": 1,
    "position_summary": "Bull case strong.",
    "points": [
        {
            "claim": "Earnings up.",
            "evidence": "Fundamentals report shows growth.",
            "cited_sources": ["analyst:fundamentals"],
        }
    ],
    "response_to_opposing": "",
    "confidence": "MEDIUM",
    "cited_sources": ["analyst:fundamentals"],
}

_BEAR_CANNED = {
    "side": "bear",
    "round_index": 1,
    "position_summary": "Valuation rich.",
    "points": [
        {
            "claim": "PE 35.",
            "evidence": "Tech RSI 78.",
            "cited_sources": ["analyst:technical"],
        }
    ],
    "response_to_opposing": "",
    "confidence": "MEDIUM",
    "cited_sources": ["analyst:technical"],
}

_DEBATE_CANNED = {
    "winning_side": "bull",
    "synthesis": "Bull carries.",
    "cited_evidence": ["fundamentals up"],
    "rounds_run": 1,
    "confidence": "MEDIUM",
    "cited_sources": ["analyst:fundamentals"],
}

_TRADER_BUY = {
    "ticker": "AAPL",
    "action": "buy",
    "size_shares_or_currency": 10.0,
    "size_units": "shares",
    "instrument": "stock",
    "order_type": "limit",
    "limit_price": 200.0,
    "stop_price": None,
    "time_in_force": "DAY",
    "rationale_summary": "Bull thesis.",
    "expected_impact": {
        "concentration_delta": "0%->0.1%",
        "cash_delta": "-$2,000",
        "tax_estimate": "$0",
    },
    "confidence": "MEDIUM",
    "cited_sources": ["analyst:fundamentals", "researcher_facilitator"],
}

_TRADER_HOLD = {**_TRADER_BUY, "action": "hold", "size_shares_or_currency": 0.0}

_RISK_APPROVE = {
    "perspective": "neutral",
    "round_index": 1,
    "verdict": "APPROVE",
    "conditions": [],
    "concerns": [
        {
            "concern": "Vol elevated",
            "evidence": "Tech RSI 78",
            "cited_sources": ["analyst:technical"],
        }
    ],
    "response_to_opposing": "",
    "confidence": "MEDIUM",
    "cited_sources": ["analyst:technical"],
}

_RISK_OUTCOME_APPROVE = {
    "consensus_verdict": "APPROVE",
    "consolidated_conditions": [],
    "dissent_summary": "",
    "rounds_run": 1,
    "confidence": "MEDIUM",
    "cited_sources": ["analyst:technical"],
}

_RISK_OUTCOME_REJECT = {
    "consensus_verdict": "REJECT",
    "consolidated_conditions": [],
    "dissent_summary": "All three reject.",
    "rounds_run": 1,
    "confidence": "MEDIUM",
    "cited_sources": ["analyst:technical"],
}

_FM_GREEN = {
    "decision": "green_light",
    "reason": "All approve.",
    "required_conditions": [],
    "post_execution_checks": [],
    "confidence": "HIGH",
    "cited_sources": ["risk_facilitator"],
}

_FM_BLOCK = {
    "decision": "block",
    "reason": "Constraint conflict.",
    "required_conditions": [],
    "post_execution_checks": [],
    "confidence": "HIGH",
    "cited_sources": ["risk_facilitator"],
}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_flow(
    *,
    trader_canned: dict = _TRADER_BUY,
    risk_canned: dict = _RISK_APPROVE,
    risk_outcome_canned: dict = _RISK_OUTCOME_APPROVE,
    fm_canned: dict = _FM_GREEN,
) -> DecisionFlow:
    return DecisionFlow(
        user_id="ariel",
        config=FlowConfig(
            debate_rounds_t1=1,
            debate_rounds_t2=1,
            debate_rounds_t3=1,
        ),
        bull_factory=lambda u: _mock_agent(BullResearcherAgent, _BULL_CANNED)(user_id=u),
        bear_factory=lambda u: _mock_agent(BearResearcherAgent, _BEAR_CANNED)(user_id=u),
        researcher_facilitator_factory=lambda u: _mock_agent(
            ResearcherFacilitatorAgent, _DEBATE_CANNED
        )(user_id=u),
        trader_factory=lambda u, t: _mock_agent(TraderAgent, trader_canned)(
            user_id=u, tier=t
        ),
        risk_officer_factory=lambda u, p: _mock_agent(RiskOfficerAgent, risk_canned)(
            user_id=u, perspective=p
        ),
        risk_facilitator_factory=lambda u: _mock_agent(
            RiskFacilitatorAgent, risk_outcome_canned
        )(user_id=u),
        fund_manager_factory=lambda u: _mock_agent(FundManagerAgent, fm_canned)(user_id=u),
    )


def _analyst_dummy() -> list[AgentReport]:
    """One analyst report for the flow's input list."""
    from pydantic import BaseModel

    class _Anonymous(BaseModel):
        agent_role: str = "fundamentals"
        cited_sources: list[str] = ["analyst:fundamentals"]
        confidence: ConfidenceBand = ConfidenceBand.MEDIUM
        report: str = "{}"

    return [
        AgentReport(
            agent_role="fundamentals",
            user_id="ariel",
            model="claude-sonnet-4-6",
            response_text="{}",
            tokens_in=10,
            tokens_out=10,
            cost_usd=0.0,
            prompt_hash="hash",
            confidence=ConfidenceBand.MEDIUM,
            output=_Anonymous(),
        )
    ]


async def _seed_user() -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t0_skips_debate_and_risk_team(engine: None) -> None:
    """T0: no debate, no risk team; trader directly to FM."""
    await _seed_user()
    flow = _make_flow()
    outcome = await flow.run(
        ticker="AAPL",
        tier=Tier.T0,
        analyst_reports=_analyst_dummy(),
        positions_summary="",
    )
    assert isinstance(outcome, ApprovedProposal)
    assert outcome.debate_outcome is None
    assert outcome.risk_outcome is None
    assert outcome.proposal.tier == "T0"
    assert outcome.proposal.id is not None


@pytest.mark.asyncio
async def test_t1_includes_one_round_debate_and_neutral_risk(engine: None) -> None:
    await _seed_user()
    flow = _make_flow()
    outcome = await flow.run(
        ticker="AAPL",
        tier=Tier.T1,
        analyst_reports=_analyst_dummy(),
    )
    assert isinstance(outcome, ApprovedProposal)
    assert outcome.debate_outcome is not None
    assert outcome.risk_outcome is not None
    assert outcome.proposal.tier == "T1"


@pytest.mark.asyncio
async def test_t2_full_stack(engine: None) -> None:
    await _seed_user()
    flow = _make_flow()
    outcome = await flow.run(
        ticker="AAPL",
        tier=Tier.T2,
        analyst_reports=_analyst_dummy(),
    )
    assert isinstance(outcome, ApprovedProposal)
    # T2 runs all 3 perspectives; verify multiple risk_officer agent_reports rows.
    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AgentReportRow).where(
                    AgentReportRow.decision_id == str(outcome.decision_run_id),
                    AgentReportRow.agent_role == "risk_officer",
                )
            )
        ).scalars().all()
    assert len(rows) >= 3


@pytest.mark.asyncio
async def test_t3_proposal_lands_in_cooling(engine: None) -> None:
    await _seed_user()
    flow = _make_flow()
    outcome = await flow.run(
        ticker="AAPL",
        tier=Tier.T3,
        analyst_reports=_analyst_dummy(),
    )
    assert isinstance(outcome, ApprovedProposal)
    assert outcome.proposal.status.value == "cooling"
    assert outcome.proposal.cooling_off_until is not None
    assert outcome.proposal.tier == "T3"


@pytest.mark.asyncio
async def test_t3_plan_critique_red_blocks(engine: None) -> None:
    """T3 with a RED finding touching the ticker → BlockedProposal."""
    await _seed_user()
    flow = _make_flow()
    outcome = await flow.run(
        ticker="NVDA",
        tier=Tier.T3,
        analyst_reports=_analyst_dummy(),
        plan_critique={
            "findings": [
                {
                    "severity": "RED",
                    "topic": "Concentration",
                    "plan_item_ref": "NVDA target 15%",
                    "summary": "NVDA over cap.",
                }
            ]
        },
    )
    assert isinstance(outcome, BlockedProposal)
    assert outcome.blocked_by == "plan_critique_red"


@pytest.mark.asyncio
async def test_trader_hold_short_circuits(engine: None) -> None:
    await _seed_user()
    flow = _make_flow(trader_canned=_TRADER_HOLD)
    outcome = await flow.run(
        ticker="AAPL",
        tier=Tier.T2,
        analyst_reports=_analyst_dummy(),
    )
    assert isinstance(outcome, BlockedProposal)
    assert outcome.blocked_by == "trader_hold"


@pytest.mark.asyncio
async def test_risk_team_reject_blocks(engine: None) -> None:
    await _seed_user()
    flow = _make_flow(risk_outcome_canned=_RISK_OUTCOME_REJECT)
    outcome = await flow.run(
        ticker="AAPL",
        tier=Tier.T2,
        analyst_reports=_analyst_dummy(),
    )
    assert isinstance(outcome, BlockedProposal)
    assert outcome.blocked_by == "risk_team"


@pytest.mark.asyncio
async def test_fund_manager_block(engine: None) -> None:
    await _seed_user()
    flow = _make_flow(fm_canned=_FM_BLOCK)
    outcome = await flow.run(
        ticker="AAPL",
        tier=Tier.T2,
        analyst_reports=_analyst_dummy(),
    )
    assert isinstance(outcome, BlockedProposal)
    assert outcome.blocked_by == "fund_manager"


@pytest.mark.asyncio
async def test_decision_run_links_proposal(engine: None) -> None:
    await _seed_user()
    flow = _make_flow()
    outcome = await flow.run(
        ticker="AAPL", tier=Tier.T2, analyst_reports=_analyst_dummy()
    )
    assert isinstance(outcome, ApprovedProposal)
    async with db_mod.get_session() as session:
        run = await session.get(DecisionRun, outcome.decision_run_id)
        assert run is not None
        assert run.proposal_id == outcome.proposal.id
        assert run.fund_manager_decision == "green_light"
        # Proposal history row written
        history = (
            await session.execute(
                select(ProposalHistory).where(
                    ProposalHistory.proposal_id == outcome.proposal.id
                )
            )
        ).scalars().all()
        assert len(history) >= 1
        # Proposal row exists
        prow = await session.get(ProposalRow, outcome.proposal.id)
        assert prow is not None
        assert prow.tier == "T2"
