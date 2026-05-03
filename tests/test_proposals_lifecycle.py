"""Proposal state-machine transition tests (SDD §10.3)."""

from __future__ import annotations

import pytest

from argosy.decisions.proposals import (
    IllegalTransitionError,
    Proposal,
    ProposalStatus,
    is_legal_transition,
    transition,
)


def _proposal(status: ProposalStatus = ProposalStatus.DRAFT) -> Proposal:
    return Proposal(
        user_id="ariel",
        ticker="AAPL",
        action="buy",
        size_shares_or_currency=10.0,
        tier="T1",
        status=status,
    )


@pytest.mark.parametrize(
    "src,dst",
    [
        # draft outs
        (ProposalStatus.DRAFT, ProposalStatus.COOLING),
        (ProposalStatus.DRAFT, ProposalStatus.AWAITING_HUMAN),
        (ProposalStatus.DRAFT, ProposalStatus.APPROVED),
        (ProposalStatus.DRAFT, ProposalStatus.BLOCKED),
        (ProposalStatus.DRAFT, ProposalStatus.CANCELLED),
        (ProposalStatus.DRAFT, ProposalStatus.EXPIRED),
        # cooling outs
        (ProposalStatus.COOLING, ProposalStatus.AWAITING_HUMAN),
        (ProposalStatus.COOLING, ProposalStatus.APPROVED),
        (ProposalStatus.COOLING, ProposalStatus.BLOCKED),
        (ProposalStatus.COOLING, ProposalStatus.CANCELLED),
        (ProposalStatus.COOLING, ProposalStatus.EXPIRED),
        # awaiting_human outs
        (ProposalStatus.AWAITING_HUMAN, ProposalStatus.APPROVED),
        (ProposalStatus.AWAITING_HUMAN, ProposalStatus.REJECTED),
        (ProposalStatus.AWAITING_HUMAN, ProposalStatus.CANCELLED),
        (ProposalStatus.AWAITING_HUMAN, ProposalStatus.EXPIRED),
        # approved outs
        (ProposalStatus.APPROVED, ProposalStatus.EXECUTED_PAPER),
        (ProposalStatus.APPROVED, ProposalStatus.EXECUTED_LIVE),
        (ProposalStatus.APPROVED, ProposalStatus.CANCELLED),
    ],
)
def test_legal_transitions(src: ProposalStatus, dst: ProposalStatus) -> None:
    assert is_legal_transition(src, dst)


@pytest.mark.parametrize(
    "src,dst",
    [
        # Terminal states have no outs
        (ProposalStatus.REJECTED, ProposalStatus.APPROVED),
        (ProposalStatus.EXECUTED_PAPER, ProposalStatus.APPROVED),
        (ProposalStatus.EXECUTED_LIVE, ProposalStatus.CANCELLED),
        (ProposalStatus.BLOCKED, ProposalStatus.AWAITING_HUMAN),
        (ProposalStatus.EXPIRED, ProposalStatus.APPROVED),
        (ProposalStatus.CANCELLED, ProposalStatus.APPROVED),
        # Skip steps
        (ProposalStatus.DRAFT, ProposalStatus.EXECUTED_PAPER),
        (ProposalStatus.DRAFT, ProposalStatus.REJECTED),
        (ProposalStatus.AWAITING_HUMAN, ProposalStatus.EXECUTED_LIVE),
        # Reverse
        (ProposalStatus.APPROVED, ProposalStatus.DRAFT),
        (ProposalStatus.AWAITING_HUMAN, ProposalStatus.DRAFT),
    ],
)
def test_illegal_transitions(src: ProposalStatus, dst: ProposalStatus) -> None:
    assert not is_legal_transition(src, dst)


def test_transition_mutates_and_returns_event() -> None:
    p = _proposal(ProposalStatus.AWAITING_HUMAN)
    evt = transition(p, dst=ProposalStatus.APPROVED, by="user:ariel", note="ok")
    assert p.status == ProposalStatus.APPROVED
    assert evt.src == ProposalStatus.AWAITING_HUMAN
    assert evt.dst == ProposalStatus.APPROVED
    assert evt.by == "user:ariel"
    assert evt.note == "ok"
    assert evt.at is not None


def test_transition_raises_on_illegal() -> None:
    p = _proposal(ProposalStatus.REJECTED)
    with pytest.raises(IllegalTransitionError):
        transition(p, dst=ProposalStatus.APPROVED, by="user:ariel")


def test_full_main_t2_lifecycle() -> None:
    """Main account T2: draft → awaiting_human → approved → executed_paper."""
    p = _proposal(ProposalStatus.DRAFT)
    transition(p, dst=ProposalStatus.AWAITING_HUMAN, by="flow")
    transition(p, dst=ProposalStatus.APPROVED, by="user:ariel")
    transition(p, dst=ProposalStatus.EXECUTED_PAPER, by="paper_fill")
    assert p.status == ProposalStatus.EXECUTED_PAPER


def test_full_t3_lifecycle_via_cooling() -> None:
    """T3: draft → cooling → awaiting_human → approved → executed_paper."""
    p = _proposal(ProposalStatus.DRAFT)
    transition(p, dst=ProposalStatus.COOLING, by="flow:t3")
    transition(p, dst=ProposalStatus.AWAITING_HUMAN, by="process_cooling")
    transition(p, dst=ProposalStatus.APPROVED, by="user:ariel:2fa")
    transition(p, dst=ProposalStatus.EXECUTED_PAPER, by="paper_fill")
    assert p.status == ProposalStatus.EXECUTED_PAPER


def test_t3_cooling_to_blocked_on_repath() -> None:
    """During cooling, an auto-pause trigger may block."""
    p = _proposal(ProposalStatus.COOLING)
    transition(p, dst=ProposalStatus.BLOCKED, by="auto_pause")
    assert p.status == ProposalStatus.BLOCKED


def test_limited_paper_short_circuit() -> None:
    """Limited acct paper: cooling → approved → executed_paper, no human."""
    p = _proposal(ProposalStatus.COOLING)
    transition(p, dst=ProposalStatus.APPROVED, by="process_cooling:limited_paper")
    transition(p, dst=ProposalStatus.EXECUTED_PAPER, by="paper_fill")
    assert p.status == ProposalStatus.EXECUTED_PAPER
