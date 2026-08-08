"""Explicit, queryable decision overrides (debate loser + confidence delta)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import DecisionOverride

log = get_logger(__name__)

_BULL_ACTIONS = frozenset({"BUY", "ADD"})
_BEAR_ACTIONS = frozenset({"SELL", "TRIM", "REDUCE"})


def debate_action_contradicts_winning_side(
    *,
    winning_side: str | None,
    trade_action: str | None,
) -> bool:
    """True when the trade direction opposes the facilitator's winning_side."""
    side = (winning_side or "").strip().lower()
    action = (trade_action or "").strip().upper()
    if side not in ("bull", "bear") or not action:
        return False
    if action in ("HOLD", "WAIT", "INSUFFICIENT_DATA"):
        return False
    if side == "bull" and action in _BEAR_ACTIONS:
        return True
    if side == "bear" and action in _BULL_ACTIONS:
        return True
    return False


def record_debate_winner_override(
    session: Session,
    *,
    user_id: str,
    decision_run_id: int,
    ticker: str,
    winning_side: str,
    trade_action: str,
    reason: str,
) -> DecisionOverride:
    row = DecisionOverride(
        user_id=user_id,
        decision_run_id=decision_run_id,
        ticker=ticker.strip().upper(),
        kind="debate_winner_contradiction",
        winning_side=winning_side,
        trade_action=trade_action,
        reason=reason,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    log.info(
        "decision_override.debate_winner",
        decision_run_id=decision_run_id,
        ticker=ticker,
        winning_side=winning_side,
        trade_action=trade_action,
    )
    return row


def record_confidence_delta(
    session: Session,
    *,
    user_id: str,
    decision_run_id: int,
    ticker: str,
    emitted_confidence: str,
    input_floor_confidence: str,
    reason: str,
) -> DecisionOverride:
    """Record that emitted confidence rose above the input floor — observation only."""
    row = DecisionOverride(
        user_id=user_id,
        decision_run_id=decision_run_id,
        ticker=ticker.strip().upper(),
        kind="confidence_delta",
        prior_confidence=input_floor_confidence,
        capped_confidence=emitted_confidence,  # column reused: emitted (not capped)
        reason=reason,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    log.info(
        "decision_override.confidence_delta",
        decision_run_id=decision_run_id,
        ticker=ticker,
        floor=input_floor_confidence,
        emitted=emitted_confidence,
    )
    return row


# Back-compat name used by earlier wiring — same observation semantics.
def record_confidence_cap_override(
    session: Session,
    *,
    user_id: str,
    decision_run_id: int,
    ticker: str,
    prior_confidence: str,
    capped_confidence: str,
    reason: str,
) -> DecisionOverride:
    return record_confidence_delta(
        session,
        user_id=user_id,
        decision_run_id=decision_run_id,
        ticker=ticker,
        emitted_confidence=capped_confidence,
        input_floor_confidence=prior_confidence,
        reason=reason,
    )


__all__ = [
    "debate_action_contradicts_winning_side",
    "record_confidence_cap_override",
    "record_confidence_delta",
    "record_debate_winner_override",
]
