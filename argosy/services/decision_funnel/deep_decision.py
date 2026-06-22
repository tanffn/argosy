"""Stage 3 — deep decision (reuse decisions/flow.py).

For each Stage-2 survivor, run the SAME full multi-agent fleet the /consult path
uses (analysts -> bull/bear -> trader -> risk team -> fund manager), producing a
fresh Buy/Sell/Hold proposal. This is PROPOSE-AND-ASK only: the funnel never
auto-executes a discretionary trade. We run at tier T2 so the full fleet + the
fund-manager integrity check always run and the proposal always lands in the
human-review queue (never auto-promoted), regardless of account class.

This is the single most expensive stage, so the orchestrator only calls it when
``decision_funnel_stage3`` is enabled AND a candidate survived triage. The
result is recorded as an immutable decision snapshot by the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from argosy.decisions.flow import ApprovedProposal, BlockedProposal, DecisionFlow
from argosy.decisions.per_ticker_analysts import (
    InsufficientAnalystQuorum,
    close_decision_run_blocked,
    open_decision_run_for_consult,
    run_per_ticker_analysts,
)
from argosy.decisions.tiers import Tier
from argosy.logging import get_logger

_log = get_logger("argosy.services.decision_funnel.deep_decision")


@dataclass(frozen=True)
class DeepDecisionOutcome:
    ticker: str
    status: Literal["approved", "blocked", "quorum_failed", "error"]
    decision_run_id: int | None = None
    proposal_id: int | None = None
    action: str | None = None
    blocked_reason: str | None = None
    blocked_by: str | None = None


async def run_deep_decision(
    *,
    user_id: str,
    ticker: str,
    positions_summary: str = "",
    user_constraints: str = "",
    account_class: str = "main",
    tier: Tier = Tier.T2,
    consult_mode: Literal["tactical_trade", "long_hold"] = "long_hold",
) -> DeepDecisionOutcome:
    """Run the full deep-decision fleet for one ticker. Never raises — returns
    a structured outcome the orchestrator records (incl. quorum / error)."""
    pre_opened = await open_decision_run_for_consult(
        user_id=user_id, ticker=ticker, tier_value=tier.value
    )
    try:
        result = await run_per_ticker_analysts(
            user_id=user_id, ticker=ticker, decision_run_id=pre_opened,
            mode=consult_mode,
        )
    except InsufficientAnalystQuorum as exc:
        await close_decision_run_blocked(
            decision_run_id=pre_opened, reason=exc.reason
        )
        _log.info("decision_funnel.deep_quorum_failed", ticker=ticker, reason=exc.reason)
        return DeepDecisionOutcome(
            ticker=ticker, status="quorum_failed", decision_run_id=pre_opened,
            blocked_reason=exc.reason, blocked_by="analyst_quorum",
        )
    except Exception as exc:  # noqa: BLE001
        await close_decision_run_blocked(
            decision_run_id=pre_opened, reason="per_ticker_analysts failure"
        )
        _log.warning("decision_funnel.deep_analysts_error", ticker=ticker, error=str(exc)[:200])
        return DeepDecisionOutcome(
            ticker=ticker, status="error", decision_run_id=pre_opened,
            blocked_reason=str(exc)[:200], blocked_by="analysts_error",
        )

    flow = DecisionFlow(user_id=user_id)
    try:
        outcome = await flow.run(
            ticker=ticker,
            tier=tier,
            analyst_reports=result.reports,
            positions_summary=positions_summary,
            user_constraints=user_constraints,
            account_class=account_class,  # type: ignore[arg-type]
            decision_run_id=pre_opened,
            persist_input_analysts=False,
            consult_mode=consult_mode,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("decision_funnel.deep_flow_error", ticker=ticker, error=str(exc)[:200])
        return DeepDecisionOutcome(
            ticker=ticker, status="error", decision_run_id=pre_opened,
            blocked_reason=str(exc)[:200], blocked_by="flow_error",
        )

    if isinstance(outcome, ApprovedProposal):
        return DeepDecisionOutcome(
            ticker=ticker, status="approved",
            decision_run_id=outcome.decision_run_id,
            proposal_id=outcome.proposal.id,
            action=outcome.proposal.action,
        )
    assert isinstance(outcome, BlockedProposal)
    return DeepDecisionOutcome(
        ticker=ticker, status="blocked",
        decision_run_id=outcome.decision_run_id,
        blocked_reason=outcome.reason, blocked_by=outcome.blocked_by,
    )


__all__ = ["DeepDecisionOutcome", "run_deep_decision"]
