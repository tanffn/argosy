"""Decisions API (Phase 3).

POST /api/decisions/run — manual ad-hoc decision flow trigger.

Takes a ticker, a tier (or 'auto' from the resolver), and a list of
analyst_report_ids that the flow will pull from `agent_reports`. Returns
the resulting decision-run id + proposal id (or block reason).

This route exists so the user can trigger a one-shot flow from the CLI
or the dashboard without waiting for a cadence to fire it.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from argosy.agent_settings import load_agent_settings
from argosy.agents.base import AgentReport, ConfidenceBand
from argosy.billing.decorators import requires_feature, requires_within_quota
from argosy.decisions.flow import (
    ApprovedProposal,
    BlockedProposal,
    DecisionFlow,
)
from argosy.decisions.tiers import Tier, TierContext, apply_override_mode, resolve_tier
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow

router = APIRouter(prefix="/decisions", tags=["decisions"])


class RunRequest(BaseModel):
    user_id: str = "ariel"
    ticker: str
    tier: str = "auto"  # 'auto' | 'T0' | 'T1' | 'T2' | 'T3'
    analyst_report_ids: list[int] = Field(default_factory=list)
    positions_summary: str = ""
    plan_critique: dict[str, Any] | None = None
    user_constraints: str = ""
    risk_caps: dict[str, Any] = Field(default_factory=dict)
    account_class: str = "main"
    # Tier-context fields used when tier='auto'. For Phase 3 the dashboard
    # passes these in directly so the resolver has something to work on
    # without requiring a full price-feed wire-up.
    proposed_value_usd: float = 0.0
    portfolio_value_usd: float = 1.0
    account_value_usd: float = 0.0
    is_plan_structural: bool = False
    crosses_concentration_cap: bool = False
    recent_red_flag: bool = False


class RunResponse(BaseModel):
    decision_run_id: int
    status: str  # 'approved' | 'blocked'
    proposal_id: int | None = None
    blocked_reason: str | None = None
    blocked_by: str | None = None
    tier: str


@router.post("/run", response_model=RunResponse)
@requires_feature("agent_fleet_full")
@requires_within_quota("monthly_decisions")
@requires_within_quota("monthly_claude_spend_usd")
async def run_decision_flow(body: RunRequest) -> RunResponse:
    settings = load_agent_settings(body.user_id)

    if body.tier == "auto":
        ctx = TierContext(
            proposed_value_usd=body.proposed_value_usd,
            portfolio_value_usd=body.portfolio_value_usd,
            account_class=body.account_class,  # type: ignore[arg-type]
            ticker=body.ticker,
            is_nvda=body.ticker.upper() == "NVDA",
            is_plan_structural=body.is_plan_structural,
            crosses_concentration_cap=body.crosses_concentration_cap,
            recent_red_flag=body.recent_red_flag,
            account_value_usd=body.account_value_usd,
        )
        auto_tier = resolve_tier(ctx, settings)
        tier = apply_override_mode(auto_tier, settings)
    else:
        try:
            tier = Tier.from_str(body.tier)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    analyst_reports = await _load_analyst_reports(body.user_id, body.analyst_report_ids)

    flow = DecisionFlow(user_id=body.user_id, settings=settings)
    outcome = await flow.run(
        ticker=body.ticker,
        tier=tier,
        analyst_reports=analyst_reports,
        positions_summary=body.positions_summary,
        plan_critique=body.plan_critique,
        user_constraints=body.user_constraints,
        risk_caps=body.risk_caps,
        account_class=body.account_class,  # type: ignore[arg-type]
    )

    if isinstance(outcome, ApprovedProposal):
        return RunResponse(
            decision_run_id=outcome.decision_run_id,
            status="approved",
            proposal_id=outcome.proposal.id,
            tier=tier.value,
        )
    assert isinstance(outcome, BlockedProposal)
    return RunResponse(
        decision_run_id=outcome.decision_run_id,
        status="blocked",
        blocked_reason=outcome.reason,
        blocked_by=outcome.blocked_by,
        tier=tier.value,
    )


async def _load_analyst_reports(user_id: str, ids: list[int]) -> list[AgentReport]:
    """Load `agent_reports` rows by id and reconstruct minimal AgentReport.

    The decision flow only reads `agent_role` + `output.model_dump()` from
    each, so we wrap the row's response_text into a generic dict-shaped
    output. This keeps the API simple without requiring the user to
    serialize full pydantic instances back over JSON.
    """
    if not ids:
        return []

    from pydantic import BaseModel

    class _Anonymous(BaseModel):
        agent_role: str = "analyst"
        cited_sources: list[str] = []
        confidence: ConfidenceBand = ConfidenceBand.MEDIUM
        # Carry the raw payload as a string so trader/researcher prompts can read it.
        report: str = ""

    async with db_mod.get_session() as session:
        rows = (
            await session.execute(
                select(AgentReportRow).where(
                    AgentReportRow.id.in_(ids), AgentReportRow.user_id == user_id
                )
            )
        ).scalars().all()
        out: list[AgentReport] = []
        for r in rows:
            try:
                payload = json.loads(r.response_text)
                if isinstance(payload, dict):
                    payload.setdefault("cited_sources", ["agent_reports"])
                    payload.setdefault("agent_role", r.agent_role)
                    obj = _Anonymous(
                        agent_role=r.agent_role,
                        cited_sources=payload.get("cited_sources", []),
                        confidence=ConfidenceBand(r.confidence)
                        if r.confidence
                        else ConfidenceBand.MEDIUM,
                        report=json.dumps(payload),
                    )
                else:
                    raise ValueError("not dict payload")
            except Exception:
                obj = _Anonymous(
                    agent_role=r.agent_role,
                    cited_sources=["agent_reports"],
                    report=r.response_text,
                )
            out.append(
                AgentReport(
                    agent_role=r.agent_role,
                    user_id=r.user_id,
                    model=r.model,
                    response_text=r.response_text,
                    tokens_in=r.tokens_in,
                    tokens_out=r.tokens_out,
                    cost_usd=float(r.cost_usd),
                    prompt_hash=r.prompt_hash,
                    confidence=ConfidenceBand(r.confidence) if r.confidence else None,
                    output=obj,
                    decision_id=r.decision_id,
                )
            )
        return out


__all__ = ["router"]
