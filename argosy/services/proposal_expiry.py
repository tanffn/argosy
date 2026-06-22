"""Proposal expiry (P0).

A funnel recommendation is time-sensitive: it was made against a specific
price / news / portfolio snapshot, and it goes stale. Rather than let an open
proposal linger and get acted on against drifted facts, every funnel proposal
carries an ``expires_at``; this sweep transitions any still-open proposal past
its TTL to ``EXPIRED`` and marks the linked immutable snapshot's human action
state accordingly.

The sweep is conservative and idempotent: it only touches proposals in the
non-terminal, awaiting states, uses the legal state machine, and records a
``ProposalHistory`` row for the audit trail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from argosy.decisions.proposals import ProposalStatus, is_legal_transition
from argosy.logging import get_logger
from argosy.state.models import DecisionSnapshot, Proposal, ProposalHistory

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

_log = get_logger("argosy.services.proposal_expiry")

# Default time-to-live for a funnel proposal. A long-hold book is not a day
# trader's blotter, but a Buy/Sell/Trim made against today's price + news must
# not survive untouched for a week. Three days is the calibration default;
# Stage-1/3 may set a tighter TTL on a high-volatility name.
DEFAULT_TTL_HOURS = 72

_OPEN_STATES = {
    ProposalStatus.DRAFT.value,
    ProposalStatus.COOLING.value,
    ProposalStatus.AWAITING_HUMAN.value,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_expiry(now: datetime | None = None, *, ttl_hours: int = DEFAULT_TTL_HOURS) -> datetime:
    """The expires_at to stamp on a freshly-created funnel proposal."""
    return (now or _utcnow()) + timedelta(hours=ttl_hours)


def expire_stale_proposals(
    session: "Session", *, user_id: str, now: datetime | None = None
) -> list[int]:
    """Transition every open, past-TTL proposal to EXPIRED. Returns the ids
    expired. Idempotent — terminal / future-dated proposals are skipped."""
    now = now or _utcnow()
    # Scoped to funnel proposals (the only ones with a TTL) so this never
    # touches a manually-created or monthly-cycle proposal even if one ever
    # carries an expires_at.
    rows = (
        session.execute(
            select(Proposal).where(
                Proposal.user_id == user_id,
                Proposal.source == "decision_funnel",
                Proposal.status.in_(_OPEN_STATES),
                Proposal.expires_at.is_not(None),
                Proposal.expires_at < now,
            )
        )
        .scalars()
        .all()
    )
    expired: list[int] = []
    for p in rows:
        try:
            src = ProposalStatus(p.status)
        except ValueError:
            continue
        if not is_legal_transition(src, ProposalStatus.EXPIRED):
            continue
        p.status = ProposalStatus.EXPIRED.value
        session.add(
            ProposalHistory(
                proposal_id=p.id,
                status=ProposalStatus.EXPIRED.value,
                transitioned_by="proposal_expiry",
                note=f"auto-expired: past TTL (expires_at={p.expires_at.isoformat()})",
            )
        )
        # Propagate to the immutable decision snapshot's lifecycle column so the
        # trace reflects the terminal human-action state.
        snaps = (
            session.execute(
                select(DecisionSnapshot).where(DecisionSnapshot.proposal_id == p.id)
            )
            .scalars()
            .all()
        )
        for s in snaps:
            if s.human_action_state == "proposed":
                s.human_action_state = "expired"
        expired.append(p.id)

    if expired:
        session.commit()
        _log.info("proposal_expiry.expired", user_id=user_id, count=len(expired), ids=expired)
    return expired


__all__ = ["DEFAULT_TTL_HOURS", "default_expiry", "expire_stale_proposals"]
