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
from argosy.services.decision_funnel.estate_kb import estate_constraints_block

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
    funnel_meta: dict | None = None,
) -> DeepDecisionOutcome:
    """Run the full deep-decision fleet for one ticker. Never raises — returns
    a structured outcome the orchestrator records (incl. quorum / error).

    ``funnel_meta`` (source/shadow/expires_at/funnel_run_id) is threaded into
    the flow so the proposal is born with its funnel lifecycle fields set
    ATOMICALLY — a shadow proposal is never briefly client-visible.
    """
    # INPUTS fix (verify-run 2026-07-08, SOFI): the stage-3 fleet must see
    # the estate/us-situs domain_knowledge — the FM previously noted "no
    # domain_knowledge file authorizing a US-estate rule was supplied" and
    # routed a US-domiciled BUY forward. Best-effort: a load failure never
    # kills the funnel (the deterministic floor below still guards).
    try:
        user_constraints = estate_constraints_block(user_constraints)
    except Exception:  # noqa: BLE001 — inputs enrichment must not crash stage 3
        _log.exception("decision_funnel.estate_kb_block_failed", ticker=ticker)
    try:
        pre_opened = await open_decision_run_for_consult(
            user_id=user_id, ticker=ticker, tier_value=tier.value
        )
    except Exception as exc:  # noqa: BLE001 — pre-open must not crash the funnel
        _log.warning("decision_funnel.deep_open_error", ticker=ticker, error=str(exc)[:200])
        return DeepDecisionOutcome(
            ticker=ticker, status="error", blocked_reason=str(exc)[:200],
            blocked_by="open_error",
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
            funnel_meta=funnel_meta,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("decision_funnel.deep_flow_error", ticker=ticker, error=str(exc)[:200])
        return DeepDecisionOutcome(
            ticker=ticker, status="error", decision_run_id=pre_opened,
            blocked_reason=str(exc)[:200], blocked_by="flow_error",
        )

    if isinstance(outcome, ApprovedProposal):
        floor_outcome = await _apply_us_situs_floor(outcome)
        if floor_outcome is not None:
            return floor_outcome
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


async def _apply_us_situs_floor(outcome: ApprovedProposal) -> DeepDecisionOutcome | None:
    """The deterministic estate/us-situs FLOOR over funnel-originated buys.

    Extends the SAME rule module that guards deploy/plan
    (``argosy.quality.plan_risk_kernel.evaluate_us_situs`` — NVDA is the one
    sanctioned US-situs name; unknown symbols fail CLOSED) with one more call
    site: a fleet-approved funnel BUY of a US-domiciled non-NVDA instrument.
    This is inviolable-arithmetic-floor territory (estate rule), NOT a
    judgment gate — it never evaluates whether the buy is a good idea.

    Returns ``None`` when the floor passes (or is not applicable — sells /
    holds are out of scope: we cannot unwind history, only gate new flows).
    On a violation the persisted proposal is flipped to ``blocked`` (with a
    ProposalHistory row recording the reason) and a blocked outcome is
    returned so the funnel trace records ``blocked_by='us_situs_floor'``.
    """
    prop = outcome.proposal
    if (prop.action or "").lower() != "buy":
        return None

    from argosy.quality.plan_risk_kernel import evaluate_us_situs

    amount = float(prop.size_shares_or_currency or 0.0) or 1.0
    result = evaluate_us_situs({}, proposed_buys={prop.ticker: amount})
    if result.ok:
        return None

    reason = "; ".join(v.detail for v in result.violations)
    _log.warning(
        "decision_funnel.us_situs_floor_blocked",
        ticker=prop.ticker, proposal_id=prop.id, reason=reason[:300],
    )
    try:
        await _mark_proposal_blocked_by_floor(
            proposal_id=prop.id,
            decision_run_id=outcome.decision_run_id,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 — must not crash the funnel; log LOUD
        _log.exception(
            "decision_funnel.us_situs_floor_persist_failed",
            ticker=prop.ticker, proposal_id=prop.id,
        )
        return DeepDecisionOutcome(
            ticker=prop.ticker, status="error",
            decision_run_id=outcome.decision_run_id, proposal_id=prop.id,
            blocked_reason=(
                "us_situs floor violation could NOT be persisted to the "
                f"proposal row — manual review required: {reason}"
            ),
            blocked_by="us_situs_floor",
        )
    return DeepDecisionOutcome(
        ticker=prop.ticker, status="blocked",
        decision_run_id=outcome.decision_run_id, proposal_id=prop.id,
        action=prop.action, blocked_reason=reason, blocked_by="us_situs_floor",
    )


async def _mark_proposal_blocked_by_floor(
    *, proposal_id: int | None, decision_run_id: int | None, reason: str
) -> None:
    """Flip the already-persisted proposal (and its decision run) to
    ``blocked`` and append the audit-trail history row. No-op when the flow
    ran with persistence skipped (``proposal_id`` falsy)."""
    if not proposal_id:
        return
    from argosy.state import db as db_mod
    from argosy.state.models import (
        DecisionRun,
        Proposal as ProposalRow,
        ProposalHistory,
    )

    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, proposal_id)
        if row is not None:
            row.status = "blocked"
            session.add(
                ProposalHistory(
                    proposal_id=proposal_id,
                    status="blocked",
                    transitioned_by="us_situs_floor",
                    note=reason[:2000],
                )
            )
        if decision_run_id:
            run = await session.get(DecisionRun, decision_run_id)
            if run is not None:
                run.status = "blocked"
        await session.commit()


__all__ = ["DeepDecisionOutcome", "run_deep_decision"]
