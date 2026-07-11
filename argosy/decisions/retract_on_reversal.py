"""Retract open proposals contradicted by a settled decision-run verdict.

Item C (2026-07-11): when a deep-decision / adjudication settles a verdict
that contradicts an OPEN proposal on the same ticker, cancel that proposal
in the same transaction as the verdict write, with a ``proposals_history``
note citing the run (shape copied from the 2026-07-10 hand-cleanups on
proposals 2/3/10).

Shadow proposals are included (they surface in /inbox too). A verdict on
ticker X never touches proposals for Y. The proposal produced by *this*
run is never retracted.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from argosy.logging import get_logger
from argosy.state.models import Proposal, ProposalHistory

_log = get_logger("argosy.decisions.retract_on_reversal")

# Client-visible / inbox-active statuses. Approved/executed/cancelled are done.
OPEN_PROPOSAL_STATUSES = frozenset({"awaiting_human", "cooling", "draft"})

# History note + transitioned_by match the 2026-07-09/10 hand-cleanup shape
# (proposals 2/3/10): "Retracted: superseded by re-adjudication run N (...)."
RETRACT_TRANSITIONED_BY = "fleet:verdict_retract"


def actions_contradicted_by(verdict: str) -> frozenset[str]:
    """Return proposal ``action`` values this verdict supersedes."""
    v = (verdict or "").strip().upper()
    if v in ("HOLD", "WAIT"):
        # HOLD/WAIT reverses both open sells and open buys on the name.
        return frozenset({"buy", "sell", "add", "trim"})
    if v in ("BUY", "ADD"):
        return frozenset({"sell", "trim"})
    if v in ("SELL", "TRIM"):
        return frozenset({"buy", "add"})
    return frozenset()


def history_note_for_retract(
    *,
    decision_run_id: int,
    verdict: str,
    detail: str = "",
) -> str:
    """Match the hand-cleanup note shape on proposals 2/3/10."""
    v = (verdict or "").strip().upper()
    body = f"trader verdict {v}"
    extra = (detail or "").strip()
    if extra:
        body = f"{body} — {extra}"
    return (
        f"Retracted: superseded by re-adjudication run {decision_run_id} "
        f"({body}). One decision = one inbox row."
    )


def retract_contradictory_open_proposals(
    session: Session,
    *,
    user_id: str,
    ticker: str,
    verdict: str,
    decision_run_id: int,
    detail: str = "",
    now: datetime | None = None,
) -> list[int]:
    """Cancel open same-ticker proposals contradicted by ``verdict``.

    Must share the caller's transaction (caller commits). Returns cancelled
    proposal ids. No-op when the verdict does not contradict any action.
    """
    contradicted = actions_contradicted_by(verdict)
    if not contradicted or not decision_run_id:
        return []

    sym = (ticker or "").strip().upper()
    if not sym:
        return []

    now = now or datetime.now(timezone.utc)
    note = history_note_for_retract(
        decision_run_id=decision_run_id, verdict=verdict, detail=detail,
    )

    rows = list(
        session.execute(
            select(Proposal).where(
                Proposal.user_id == user_id,
                Proposal.ticker == sym,
                Proposal.status.in_(tuple(OPEN_PROPOSAL_STATUSES)),
            )
        ).scalars().all()
    )

    cancelled: list[int] = []
    for row in rows:
        # Never retract the proposal this same run just wrote.
        if row.decision_run_id == decision_run_id:
            continue
        action = (row.action or "").strip().lower()
        if action not in contradicted:
            continue
        row.status = "cancelled"
        row.updated_at = now
        session.add(
            ProposalHistory(
                proposal_id=row.id,
                status="cancelled",
                transitioned_at=now,
                transitioned_by=RETRACT_TRANSITIONED_BY,
                note=note,
            )
        )
        cancelled.append(row.id)
        _log.info(
            "retract_on_reversal.cancelled",
            proposal_id=row.id,
            ticker=sym,
            action=action,
            verdict=verdict,
            decision_run_id=decision_run_id,
            shadow=int(getattr(row, "shadow", 0) or 0),
        )

    if cancelled:
        session.flush()
    return cancelled


__all__ = [
    "OPEN_PROPOSAL_STATUSES",
    "RETRACT_TRANSITIONED_BY",
    "actions_contradicted_by",
    "history_note_for_retract",
    "retract_contradictory_open_proposals",
]
