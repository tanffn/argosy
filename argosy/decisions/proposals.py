"""Proposals lifecycle (SDD §10, Phase 3).

Owns the `Proposal` pydantic model and the legal state-machine
transitions. The DB ORM `Proposal` row lives in `state.models`; this
module is the source of truth for *which* transitions are legal and
for the side-effects (history rows, approval rows, WebSocket events).

State machine (SDD §10.3):

    draft ──┬──> awaiting_human ──> approved ──> executed_paper
            │                  │              └─> executed_live
            │                  └─> rejected
            ├──> cooling ──> awaiting_human (T2/T3 main) ──...
            │            └─> approved (limited/paper, auto-promotion)
            ├──> blocked
            ├──> expired
            └─> cancelled
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.trader import ExpectedImpact


class ProposalStatus(str, enum.Enum):
    DRAFT = "draft"
    COOLING = "cooling"
    AWAITING_HUMAN = "awaiting_human"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED_PAPER = "executed_paper"
    EXECUTED_LIVE = "executed_live"
    BLOCKED = "blocked"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# Legal state-machine transitions. Source-of-truth.
# Anything not in this dict is illegal.
_LEGAL_TRANSITIONS: dict[ProposalStatus, set[ProposalStatus]] = {
    ProposalStatus.DRAFT: {
        ProposalStatus.COOLING,
        ProposalStatus.AWAITING_HUMAN,
        ProposalStatus.APPROVED,  # T0/T1 limited+live auto-execute path
        ProposalStatus.BLOCKED,
        ProposalStatus.CANCELLED,
        ProposalStatus.EXPIRED,
    },
    ProposalStatus.COOLING: {
        ProposalStatus.AWAITING_HUMAN,  # T3 cooling-off advances to human queue
        ProposalStatus.APPROVED,  # limited-acct paper short-circuit
        ProposalStatus.BLOCKED,  # auto-pause triggered re-check failure
        ProposalStatus.CANCELLED,
        ProposalStatus.EXPIRED,
    },
    ProposalStatus.AWAITING_HUMAN: {
        ProposalStatus.APPROVED,
        ProposalStatus.REJECTED,
        ProposalStatus.CANCELLED,
        ProposalStatus.EXPIRED,
    },
    ProposalStatus.APPROVED: {
        ProposalStatus.EXECUTED_PAPER,
        ProposalStatus.EXECUTED_LIVE,
        ProposalStatus.CANCELLED,  # last-second cancel before broker call
    },
    # Terminal states
    ProposalStatus.REJECTED: set(),
    ProposalStatus.EXECUTED_PAPER: set(),
    ProposalStatus.EXECUTED_LIVE: set(),
    ProposalStatus.BLOCKED: set(),
    ProposalStatus.EXPIRED: set(),
    ProposalStatus.CANCELLED: set(),
}


def is_legal_transition(src: ProposalStatus, dst: ProposalStatus) -> bool:
    return dst in _LEGAL_TRANSITIONS.get(src, set())


class IllegalTransitionError(ValueError):
    """Raised when caller attempts an illegal status change."""

    def __init__(self, src: ProposalStatus, dst: ProposalStatus) -> None:
        super().__init__(
            f"illegal proposal transition: {src.value} -> {dst.value}"
        )
        self.src = src
        self.dst = dst


def assert_legal(src: ProposalStatus, dst: ProposalStatus) -> None:
    if not is_legal_transition(src, dst):
        raise IllegalTransitionError(src, dst)


# ----------------------------------------------------------------------
# Pydantic model
# ----------------------------------------------------------------------


class Proposal(BaseModel):
    """Proposal record. Mirrors `state.models.Proposal` ORM but is the
    transport object: pydantic-validated, serialized to JSON for the API,
    used in tests without DB.
    """

    id: int | None = None
    user_id: str
    ticker: str
    action: Literal["buy", "sell", "hold"]
    size_shares_or_currency: float
    size_units: Literal["shares", "currency"] = "shares"
    instrument: Literal["stock", "etf", "option"] = "stock"
    order_type: Literal["market", "limit", "stop", "stop-limit"] = "market"
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: Literal["DAY", "GTC", "IOC", "FOK"] = "DAY"
    tier: Literal["T0", "T1", "T2", "T3"]
    account_class: Literal["main", "limited"] = "main"
    status: ProposalStatus = ProposalStatus.DRAFT
    rationale_summary: str = ""
    expected_impact: ExpectedImpact = Field(default_factory=ExpectedImpact)
    confidence: str = "MEDIUM"
    cooling_off_until: datetime | None = None
    decision_run_id: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TransitionEvent:
    """One transition record persisted to `proposals_history`."""

    src: ProposalStatus
    dst: ProposalStatus
    at: datetime
    by: str
    note: str = ""


def transition(
    proposal: Proposal,
    *,
    dst: ProposalStatus,
    by: str,
    note: str = "",
    now: datetime | None = None,
) -> TransitionEvent:
    """Mutate `proposal.status` to `dst` and return the audit event.

    Raises IllegalTransitionError on a non-permitted transition.
    """
    src = proposal.status
    assert_legal(src, dst)
    moment = now or datetime.now(timezone.utc)
    proposal.status = dst
    proposal.updated_at = moment
    return TransitionEvent(src=src, dst=dst, at=moment, by=by, note=note)


__all__ = [
    "IllegalTransitionError",
    "Proposal",
    "ProposalStatus",
    "TransitionEvent",
    "assert_legal",
    "is_legal_transition",
    "transition",
]
