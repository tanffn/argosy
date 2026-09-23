from __future__ import annotations

from datetime import UTC, datetime

import pytest

from argosy.agents.base import AgentReport, ConfidenceBand
from argosy.agents.trader import TraderProposal
from argosy.decisions.flow import ApprovedProposal, DecisionFlow
from argosy.decisions.tiers import Tier
from argosy.adapters.brokers.types import Fill as BrokerFill
from argosy.execution.reconcile import persist_broker_fill
from argosy.execution.router import ExecutionRouter
from argosy.execution.audit import write_paper_fill
from argosy.services.chat_advisor.execution_policy import AnalysisOnlyViolation
from argosy.services.chat_advisor.analysis_dispatch import AnalysisDispatcher
from argosy.state import db as db_mod
from argosy.state.models import DecisionRun, Proposal, User, Verdict


async def _seed(*, status="approved", account_class="limited", tier="T0") -> int:
    async with db_mod.get_session() as session:
        session.add(User(id="chat-user"))
        await session.flush()
        run = DecisionRun(
            user_id="chat-user",
            ticker="QURE",
            tier=tier,
            execution_policy="analysis_only",
            status="approved",
        )
        session.add(run)
        await session.flush()
        proposal = Proposal(
            user_id="chat-user",
            ticker="QURE",
            action="buy",
            size_shares_or_currency=1,
            size_units="shares",
            instrument="stock",
            order_type="limit",
            limit_price=10,
            time_in_force="DAY",
            tier=tier,
            account_class=account_class,
            account_id="ibkr_argonaut",
            status=status,
            rationale_summary="analysis only",
            expected_impact_json="{}",
            confidence="MEDIUM",
            decision_run_id=run.id,
        )
        session.add(proposal)
        await session.commit()
        return proposal.id


@pytest.mark.asyncio
async def test_highest_autonomy_cannot_auto_approve_or_execute(engine):
    draft_id = await _seed(status="draft")
    router = ExecutionRouter(user_id="chat-user", adapter_factories={})
    assert await router.auto_execute_if_eligible(draft_id) is None
    async with db_mod.get_session() as session:
        assert (await session.get(Proposal, draft_id)).status == "draft"


@pytest.mark.asyncio
async def test_preopened_analysis_run_is_authoritative_when_policy_omitted(engine):
    async with db_mod.get_session() as session:
        session.add(User(id="chat-user"))
        await session.flush()
        run = DecisionRun(
            user_id="chat-user", ticker="QURE", tier="T0",
            execution_policy="analysis_only", status="running",
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    proposal = TraderProposal.model_validate({
        "ticker": "QURE", "action": "buy", "size_shares_or_currency": 1,
        "size_units": "shares", "instrument": "stock", "order_type": "limit",
        "limit_price": 10, "stop_price": 8, "time_in_force": "DAY",
        "rationale_summary": "test", "expected_impact": {
            "concentration_delta": "small", "cash_delta": "-$10", "tax_estimate": "$0"
        }, "confidence": "MEDIUM", "cited_sources": ["test:fact"],
    })
    report = AgentReport(
        agent_role="trader", user_id="chat-user", model="test",
        response_text="{}", tokens_in=1, tokens_out=1, cost_usd=0,
        prompt_hash="test", confidence=ConfidenceBand.MEDIUM, output=proposal,
    )

    class Trader:
        async def run(self, **kwargs):
            return report

    flow = DecisionFlow(
        user_id="chat-user", trader_factory=lambda user_id, tier: Trader()
    )
    outcome = await flow.run(
        ticker="QURE", tier=Tier.T0, analyst_reports=[],
        account_class="limited", decision_run_id=run_id,
        persist_input_analysts=False,
        # Deliberately omit execution_policy: persisted policy must win.
    )
    assert isinstance(outcome, ApprovedProposal)
    assert outcome.proposal.status == "awaiting_human"


@pytest.mark.asyncio
async def test_analysis_only_cannot_reach_broker_or_fill_sink(engine):
    proposal_id = await _seed(status="approved")
    router = ExecutionRouter(user_id="chat-user", adapter_factories={})
    result = await router.execute(proposal_id)
    assert result.status == "rejected"
    assert result.broker == "(analysis_only)"

    fill = BrokerFill(
        proposal_id=proposal_id,
        broker="ibkr",
        broker_order_id="order-1",
        external_fill_id="fill-1",
        account_id="ibkr_argonaut",
        ticker="QURE",
        action="buy",
        quantity=1,
        price=10,
        commission=0,
        filled_at=datetime.now(UTC),
        paper=False,
    )
    async with db_mod.get_session() as session:
        with pytest.raises(AnalysisOnlyViolation, match="fill recording"):
            await persist_broker_fill(
                session,
                user_id="chat-user",
                proposal_id=proposal_id,
                account_id="ibkr_argonaut",
                fill=fill,
            )
        with pytest.raises(AnalysisOnlyViolation, match="paper fill recording"):
            await write_paper_fill(
                user_id="chat-user",
                broker="ibkr",
                ticker="QURE",
                action="buy",
                quantity=1,
                price=10,
                proposal_id=proposal_id,
                session=session,
            )


@pytest.mark.asyncio
async def test_new_run_never_inherits_old_settled_verdict_text(engine):
    async with db_mod.get_session() as session:
        session.add(User(id="chat-user"))
        session.add(Verdict(
            user_id="chat-user", subject="QURE", verdict="WAIT",
            conviction="LOW", reasoning_md="old rationale",
            source_decision_run_id=11, settled=True,
        ))
        await session.commit()

    class TenantStore:
        household_user_id = "chat-user"

    dispatcher = AnalysisDispatcher(TenantStore(), auto_schedule=False)
    fresh = await dispatcher._canonical_result(
        ticker="QURE", run_id=12, proposal_id=None,
    )
    defended = await dispatcher._canonical_result(
        ticker="QURE", run_id=11, proposal_id=None, allow_standing=True,
    )
    assert fresh["verdict"] is None
    assert fresh["rationale"] is None
    assert defended["verdict"] == "WAIT"
    assert defended["rationale"] == "old rationale"
